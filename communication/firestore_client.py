"""Accès direct à Firestore (sans passer par l'API du Balanceur).

Même interface que `CentralClient` (heartbeat, next_operation, post_event, post_result,
operation_status), pour que le reste de l'Extracteur ne change pas. Utilise l'API REST
Firestore avec la clé Web du projet (bibliothèque standard uniquement) ; fonctionne tant
que les règles Firestore autorisent l'accès (cas actuel).

Dans ce mode, c'est un humain qui tranche depuis le back-office : une opération exécutée
passe en « à vérifier » (needs_review). L'Extracteur n'écrit donc jamais « réussi ».
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from communication.client import CentralError
from config import VERSION
from utils.logger import get_logger

log = get_logger("firestore")

OPERATIONS = "operations"
EXTRACTORS = "extractors"
EVENTS = "operation_events"
CATALOG = "discovered_catalog"

# Motifs « à vérifier » selon le bilan rapporté par l'exécuteur.
_REVIEW_REASON = {
    "EXECUTED": "execute_a_verifier",
    "NOT_EXECUTED": "non_execute",
    "INTERRUPTED": "interrompu",
}


# --- Encodage / décodage du format Firestore ---------------------------------

def _enc(value: Any) -> dict[str, Any]:
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_enc(item) for item in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: _enc(v) for k, v in value.items()}}}
    return {"stringValue": str(value)}


def _enc_fields(data: dict[str, Any]) -> dict[str, Any]:
    return {key: _enc(value) for key, value in data.items()}


def _dec(value: dict[str, Any]) -> Any:
    if "nullValue" in value:
        return None
    if "booleanValue" in value:
        return value["booleanValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return value["doubleValue"]
    if "stringValue" in value:
        return value["stringValue"]
    if "timestampValue" in value:
        return value["timestampValue"]
    if "arrayValue" in value:
        return [_dec(item) for item in value["arrayValue"].get("values", [])]
    if "mapValue" in value:
        return {k: _dec(v) for k, v in value["mapValue"].get("fields", {}).items()}
    return None


def _dec_fields(document: dict[str, Any]) -> dict[str, Any]:
    return {key: _dec(value) for key, value in (document.get("fields") or {}).items()}


class FirestoreClient:
    def __init__(self, project_id: str, api_key: str, extractor_id: str, operators: list[str],
                 timeout: float = 15.0, lease_seconds: int = 180, max_attempts: int = 3, name: str = "",
                 base: str = "https://firestore.googleapis.com/v1", transport=None):
        self.project_id = project_id
        self.api_key = api_key
        self.extractor_id = extractor_id
        self.operators = [op.lower() for op in operators]
        self.timeout = timeout
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.name = name or extractor_id
        self.root = f"{base}/projects/{project_id}/databases/(default)/documents"
        self._transport = transport or self._http
        self._registered = False

    # --- Transport HTTP ----------------------------------------------------------

    def _http(self, method: str, url: str, body: Any) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"_raw": raw[:300].decode("utf-8", "replace")}
            return exc.code, parsed
        except (urllib.error.URLError, OSError) as exc:
            raise CentralError(f"Firestore injoignable : {getattr(exc, 'reason', exc)}")

    def _url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self.root}{path}{sep}key={urllib.parse.quote(self.api_key)}"

    def _request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        return self._transport(method, self._url(path), body)

    @staticmethod
    def _is_precondition(status: int, data: Any) -> bool:
        return status == 409 or (status == 400 and isinstance(data, dict)
                                 and "FAILED_PRECONDITION" in json.dumps(data.get("error", {})))

    def _raise(self, status: int, data: Any, what: str) -> None:
        message = data.get("error", {}).get("message", data) if isinstance(data, dict) else data
        raise CentralError(f"Firestore {what} : HTTP {status} {message}", status=status,
                           retryable=status >= 500 or status == 429)

    def _doc_name(self, collection: str, doc_id: str) -> str:
        return f"projects/{self.project_id}/databases/(default)/documents/{collection}/{doc_id}"

    # --- Écriture (commit, avec transformation d'historique) ---------------------

    def _write(self, collection: str, doc_id: str, fields: dict[str, Any], *,
               history: dict[str, Any] | None = None, must_not_exist: bool = False,
               update_time: str | None = None) -> tuple[int, Any]:
        write: dict[str, Any] = {
            "update": {"name": self._doc_name(collection, doc_id), "fields": _enc_fields(fields)},
            "updateMask": {"fieldPaths": list(fields.keys())},
        }
        if history is not None:
            write["updateTransforms"] = [{
                "fieldPath": "history",
                "appendMissingElements": {"values": [_enc(history)]},
            }]
        if must_not_exist:
            write["currentDocument"] = {"exists": False}
        elif update_time is not None:
            write["currentDocument"] = {"updateTime": update_time}
        return self._request("POST", ":commit", {"writes": [write]})

    def _commit(self, writes: list[dict[str, Any]]) -> tuple[int, Any]:
        return self._request("POST", ":commit", {"writes": writes})

    def _history(self, event: str, note: str = "") -> dict[str, Any]:
        return {"atMs": _now_ms(), "event": event, "by": f"extractor:{self.extractor_id}",
                "note": str(note or "")[:300]}

    # --- Interface (identique à CentralClient) -----------------------------------

    def heartbeat(self, payload: dict[str, Any]) -> Any:
        self._ensure_registered()
        fields = {
            "lastSeenAtMs": _now_ms(),
            "lastStatus": str(payload.get("status") or ""),
            "currentOperationId": str(payload.get("currentOperation") or ""),
            "pendingEvents": int(payload.get("pendingEvents") or 0),
            "deviceInfo": payload.get("deviceInfo"),
            "health": payload.get("health"),
            "version": VERSION,
            "updatedAtMs": _now_ms(),
        }
        status, data = self._write(EXTRACTORS, self.extractor_id, fields)
        if status != 200:
            self._raise(status, data, "heartbeat")
        return {"ok": True}

    def _ensure_registered(self) -> None:
        if self._registered:
            return
        status, data = self._request("GET", f"/{EXTRACTORS}/{self.extractor_id}")
        if status == 200:
            self._registered = True
            return
        if status != 404:
            self._raise(status, data, "lecture extracteur")
        # Auto-enregistrement : l'extracteur se déclare s'il n'a pas été créé au back-office.
        now = _now_ms()
        fields = {
            "name": self.name, "operators": self.operators, "proofMode": "manual",
            "status": "active", "currentOperationId": "", "lastSeenAtMs": now,
            "lastStatus": "", "deviceInfo": None, "health": None, "pendingEvents": 0,
            "createdBy": "self", "createdAtMs": now, "updatedAtMs": now,
        }
        wstatus, wdata = self._write(EXTRACTORS, self.extractor_id, fields)
        if wstatus != 200:
            self._raise(wstatus, wdata, "auto-enregistrement")
        log.info("Extracteur %s auto-enregistré dans Firestore", self.extractor_id)
        self._registered = True

    def next_operation(self) -> dict[str, Any] | None:
        if not self.operators:
            return None
        self.reclaim_stalled()
        rows = self._query_status("queued")
        candidates = []
        for doc in rows:
            fields = _dec_fields(doc)
            if fields.get("operator") in self.operators:
                candidates.append((fields.get("createdAtMs") or 0, doc, fields))
        candidates.sort(key=lambda item: item[0])

        for _, doc, fields in candidates:
            claimed = self._claim(doc, fields)
            if claimed:
                return claimed
        return None

    def _query_status(self, status_value: str, limit: int = 25) -> list[dict[str, Any]]:
        query = {"structuredQuery": {
            "from": [{"collectionId": OPERATIONS}],
            "where": {"fieldFilter": {"field": {"fieldPath": "status"},
                                      "op": "EQUAL", "value": {"stringValue": status_value}}},
            "limit": limit,
        }}
        status, data = self._request("POST", ":runQuery", query)
        if status != 200:
            self._raise(status, data, "lecture file")
        return [row["document"] for row in data if isinstance(row, dict) and row.get("document")]

    # Reprise des opérations bloquées : une opération « assigned » dont le bail a expiré
    # (extracteur devenu muet / planté) est remise en file, ou passée « à vérifier » après
    # le plafond d'essais. Un extracteur vivant renouvelle son bail (renew_lease) et n'est
    # donc jamais repris à tort.
    def reclaim_stalled(self) -> int:
        now = _now_ms()
        reclaimed = 0
        for doc in self._query_status("assigned"):
            fields = _dec_fields(doc)
            if fields.get("operator") not in self.operators:
                continue
            if int(fields.get("leaseExpiresAtMs") or 0) > now:
                continue  # bail encore valide : l'extracteur est présumé vivant
            doc_id = doc["name"].rsplit("/", 1)[-1]
            maxed = int(fields.get("attempts") or 0) >= self.max_attempts
            updates = ({"status": "needs_review", "needsReviewReason": "lease_expired_max_attempts",
                        "leaseExpiresAtMs": 0, "updatedAtMs": now}
                       if maxed else
                       {"status": "queued", "assignedTo": "", "leaseExpiresAtMs": 0, "updatedAtMs": now})
            status, data = self._write(OPERATIONS, doc_id, updates,
                                       history=self._history("lease_expired_review" if maxed else "lease_expired_requeued"),
                                       update_time=doc.get("updateTime"))
            if status == 200:
                reclaimed += 1
                log.warning("Opération %s reprise (bail expiré%s)", doc_id, ", plafond atteint" if maxed else "")
            elif not self._is_precondition(status, data):
                log.warning("Reprise de %s impossible : HTTP %s", doc_id, status)
        return reclaimed

    def renew_lease(self, operation_id: str) -> None:
        """Prolonge le bail de l'opération en cours (appelé par le battement de cœur)."""
        now = _now_ms()
        try:
            self._write(OPERATIONS, operation_id,
                        {"leaseExpiresAtMs": now + self.lease_seconds * 1000, "updatedAtMs": now})
        except CentralError as exc:
            log.debug("Renouvellement du bail de %s impossible : %s", operation_id, exc)

    def _claim(self, doc: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any] | None:
        doc_id = doc["name"].rsplit("/", 1)[-1]
        now = _now_ms()
        updates = {
            "status": "assigned",
            "assignedTo": self.extractor_id,
            "attempts": int(fields.get("attempts") or 0) + 1,
            "leaseExpiresAtMs": now + self.lease_seconds * 1000,
            "updatedAtMs": now,
        }
        status, data = self._write(OPERATIONS, doc_id, updates,
                                   history=self._history("assigned"),
                                   update_time=doc.get("updateTime"))
        if status == 200:
            log.info("Opération %s confiée (Firestore direct)", doc_id)
            return self._operation_view(doc_id, fields)
        if self._is_precondition(status, data):
            return None  # déjà prise par un autre extracteur
        self._raise(status, data, "attribution")

    @staticmethod
    def _operation_view(doc_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        view = {
            "operationId": doc_id,
            "operator": fields.get("operator"),
            "type": fields.get("type"),
            "ussdCode": fields.get("ussdCode"),
            "beneficiary": fields.get("beneficiary"),
            "amount": fields.get("amount"),
            "parameters": fields.get("parameters") or {},
        }
        return view

    def post_event(self, event: dict[str, Any]) -> None:
        event_id = str(event.get("eventId"))
        fields = {
            "extractorId": self.extractor_id,
            "operationId": event.get("operationId") or "",
            "type": event.get("type") or "",
            "seq": int(event.get("seq") or 0),
            "occurredAt": event.get("occurredAt") or "",
            "payload": event.get("payload") or {},
            "receivedAtMs": _now_ms(),
            "createdAtMs": _now_ms(),
        }
        status, data = self._write(EVENTS, event_id, fields, must_not_exist=True)
        if status == 200:
            if event.get("type") == "CATALOG_DISCOVERED":
                self._ingest_catalog(event.get("payload") or {})
            return
        if self._is_precondition(status, data):
            return  # doublon : déjà enregistré
        self._raise(status, data, "événement")

    def post_result(self, operation_id: str, result: dict[str, Any]) -> None:
        outcome = str(result.get("outcome") or "")
        fields = {
            "status": "needs_review",
            "needsReviewReason": _REVIEW_REASON.get(outcome, "a_verifier"),
            "result": _sanitize_result(result),
            "resultReceivedAtMs": _now_ms(),
            "resultBy": self.extractor_id,
            "leaseExpiresAtMs": 0,
            "updatedAtMs": _now_ms(),
        }
        status, data = self._write(OPERATIONS, operation_id, fields,
                                   history=self._history("result_received", outcome))
        if status != 200:
            self._raise(status, data, "bilan")

    def operation_status(self, operation_id: str) -> dict[str, Any] | None:
        status, data = self._request("GET", f"/{OPERATIONS}/{operation_id}")
        if status == 404:
            return None
        if status != 200:
            self._raise(status, data, "état opération")
        fields = _dec_fields(data)
        return {"operationId": operation_id, "status": fields.get("status"),
                "assignedTo": fields.get("assignedTo")}

    # --- Catalogue découvert -----------------------------------------------------

    def _ingest_catalog(self, payload: dict[str, Any]) -> None:
        operator = str(payload.get("operator") or "?")
        offers = payload.get("catalog") or []
        if not offers:
            return
        now = _now_ms()
        writes = []
        for offer in offers[:200]:
            signature = _sha256(f"{operator}:{offer.get('name')}:{offer.get('price')}:"
                                f"{'/'.join(str(p) for p in offer.get('path') or [])}")[:32]
            fields = {
                "operator": operator,
                "name": str(offer.get("name") or "")[:160],
                "price": offer.get("price"),
                "priceCurrency": offer.get("priceCurrency") or "XOF",
                "volume": offer.get("volume"),
                "volumeUnit": offer.get("volumeUnit"),
                "validity": offer.get("validity"),
                "validityUnit": offer.get("validityUnit"),
                "path": offer.get("path") or [],
                "optionKey": offer.get("optionKey"),
                "raw": str(offer.get("raw") or "")[:300],
                "rootCode": str(payload.get("rootCode") or "")[:40],
                "discoveredBy": self.extractor_id,
                "discoveredAtMs": now,
                "updatedAtMs": now,
            }
            writes.append({
                "update": {"name": self._doc_name(CATALOG, f"{operator}_{signature}"),
                           "fields": _enc_fields(fields)},
                "updateMask": {"fieldPaths": list(fields.keys())},
            })
        status, data = self._commit(writes)
        if status != 200:
            log.warning("Catalogue non enregistré : HTTP %s", status)
        else:
            log.info("Catalogue découvert enregistré (%d offre(s))", len(writes))


def _sanitize_result(result: dict[str, Any]) -> dict[str, Any]:
    ussd = result.get("ussd") or {}
    return {
        "outcome": str(result.get("outcome") or ""),
        "reason": str(result.get("reason") or ""),
        "message": str(result.get("message") or "")[:300],
        "mode": str(result.get("mode") or ""),
        "ussdStatus": str(ussd.get("status") or ""),
        "ussd": ussd or None,
        "sms": result.get("sms"),
        "ussdSent": result.get("ussdSent", bool(ussd)),
    }


# Import tardif pour éviter une dépendance circulaire à l'import du module.
def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def _sha256(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()

"""Client HTTP du Central (Balanceur). Bibliothèque standard uniquement.

Routes (détail dans docs/PROTOCOLE.md), toutes sous /extractors/{extractorId} :
  POST heartbeat                       état de l'Extracteur
  GET  operations/next                 prochaine opération (204 = rien à faire)
  POST events                          un événement observé
  POST operations/{id}/result          bilan d'exécution d'une opération
  GET  operations/{id}                 état connu du Central (reprise après coupure)
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from config import VERSION

RETRYABLE_STATUSES = {408, 425, 429}


class CentralError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class CentralClient:
    def __init__(self, base_url: str, token: str, extractor_id: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.extractor_id = extractor_id
        self.timeout = timeout

    def _path(self, *parts: str) -> str:
        quoted = "/".join(urllib.parse.quote(part, safe="") for part in ("extractors", self.extractor_id, *parts))
        return f"{self.base_url}/{quoted}"

    def _request(self, method: str, url: str, body: Any = None) -> tuple[int, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Extractor-Id": self.extractor_id,
            "User-Agent": f"motrans-extractor/{VERSION}",
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            retryable = exc.code >= 500 or exc.code in RETRYABLE_STATUSES
            raise CentralError(f"{method} {url} : HTTP {exc.code} {detail}", exc.code, retryable) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise CentralError(f"{method} {url} : {getattr(exc, 'reason', exc)}") from exc
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CentralError(f"{method} {url} : réponse non JSON", status) from exc

    def heartbeat(self, payload: dict[str, Any]) -> Any:
        return self._request("POST", self._path("heartbeat"), payload)[1]

    def next_operation(self) -> dict[str, Any] | None:
        status, data = self._request("GET", self._path("operations", "next"))
        if status == 204 or not data:
            return None
        if isinstance(data, dict) and "operation" in data:
            return data["operation"] or None
        return data

    def post_event(self, event: dict[str, Any]) -> None:
        self._request("POST", self._path("events"), event)

    def post_result(self, operation_id: str, result: dict[str, Any]) -> None:
        self._request("POST", self._path("operations", operation_id, "result"), result)

    def operation_status(self, operation_id: str) -> dict[str, Any] | None:
        return self._request("GET", self._path("operations", operation_id))[1]

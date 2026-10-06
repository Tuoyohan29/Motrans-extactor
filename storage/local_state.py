"""Petit stockage local (un fichier JSON) pour survivre à une coupure.

On y garde :
- l'identifiant de l'Extracteur ;
- l'opération en cours et sa phase (pour savoir, au redémarrage, si l'USSD a pu partir) ;
- les dernières opérations déjà exécutées (pour ne jamais exécuter deux fois la même) ;
- le curseur SMS (dernier SMS déjà vu) ;
- la file d'attente des événements pas encore livrés au Central.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from utils.logger import get_logger

log = get_logger("storage")

MAX_EXECUTED = 200
MAX_OUTBOX = 5000

DEFAULTS: dict[str, Any] = {
    "extractor_id": None,
    "install_id": None,
    "local_status": None,
    "current_operation": None,
    "last_operation": None,
    "last_event": None,
    "executed": [],
    "sms_cursor": None,
    "event_seq": 0,
    "outbox": [],
}


class LocalState:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        data = copy.deepcopy(DEFAULTS)
        if not self.path.exists():
            return data
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict):
                raise ValueError("contenu inattendu")
        except (OSError, ValueError) as exc:
            backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            log.error("État local illisible (%s), copie dans %s et redémarrage à vide", exc, backup.name)
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return data
        data.update(stored)
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            value = self._data.get(key, default)
            return copy.deepcopy(value)

    def update(self, **values: Any) -> None:
        with self._lock:
            self._data.update(values)
            self._save()

    # --- Opération en cours ------------------------------------------------------

    def set_current(self, operation: dict[str, Any] | None) -> None:
        self.update(current_operation=operation)

    def set_phase(self, phase: str) -> None:
        with self._lock:
            current = self._data.get("current_operation")
            if current is not None:
                current["phase"] = phase
                current["phaseAt"] = time.time()
                self._save()

    # --- Opérations déjà exécutées -----------------------------------------------

    def executed_result(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            for entry in self._data["executed"]:
                if entry["operationId"] == operation_id:
                    return copy.deepcopy(entry["result"])
        return None

    def mark_executed(self, operation_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            executed = [e for e in self._data["executed"] if e["operationId"] != operation_id]
            executed.append({"operationId": operation_id, "result": result, "at": time.time()})
            self._data["executed"] = executed[-MAX_EXECUTED:]
            self._data["current_operation"] = None
            self._data["last_operation"] = {"operationId": operation_id, "finishedAt": time.time()}
            self._save()

    # --- Numérotation et file d'attente des envois ---------------------------------

    def next_seq(self) -> int:
        with self._lock:
            self._data["event_seq"] += 1
            self._save()
            return self._data["event_seq"]

    def outbox_push(self, item: dict[str, Any]) -> None:
        with self._lock:
            outbox = self._data["outbox"]
            outbox.append(item)
            if len(outbox) > MAX_OUTBOX:
                dropped = len(outbox) - MAX_OUTBOX
                del outbox[:dropped]
                log.error("File d'attente pleine : %d envoi(s) le(s) plus ancien(s) abandonné(s)", dropped)
            self._save()

    def outbox_peek(self) -> dict[str, Any] | None:
        with self._lock:
            outbox = self._data["outbox"]
            return copy.deepcopy(outbox[0]) if outbox else None

    def outbox_pop(self) -> None:
        with self._lock:
            if self._data["outbox"]:
                self._data["outbox"].pop(0)
                self._save()

    def outbox_size(self) -> int:
        with self._lock:
            return len(self._data["outbox"])

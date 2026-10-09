"""Battement de cœur : signale régulièrement au Central que l'Extracteur est vivant.

Tourne dans un fil séparé pour continuer pendant une opération (le Central sait ainsi
qu'un Extracteur BUSY n'est pas mort). À chaque battement, l'état de santé du téléphone
est revérifié ; un problème bloquant passe l'Extracteur en ERROR.
"""

from __future__ import annotations

import threading
from typing import Any

from communication.client import CentralError
from config import VERSION
from utils.helpers import utc_now_iso
from utils.logger import get_logger

log = get_logger("heartbeat")


class Heartbeat(threading.Thread):
    def __init__(self, client, state_manager, health_monitor, device_info: dict[str, Any],
                 local_state, extractor_id: str, interval: float):
        super().__init__(name="heartbeat", daemon=True)
        self.client = client
        self.state_manager = state_manager
        self.health_monitor = health_monitor
        self.device_info = device_info
        self.local_state = local_state
        self.extractor_id = extractor_id
        self.interval = interval
        self._stop_event = threading.Event()
        self.last_health: dict[str, Any] = {}
        self._ok: bool | None = None

    def payload(self) -> dict[str, Any]:
        snapshot = self.state_manager.snapshot()
        return {
            "extractorId": self.extractor_id,
            "status": snapshot["status"],
            "lastSeen": utc_now_iso(),
            "currentOperation": snapshot["currentOperation"],
            "errors": snapshot["errors"],
            "deviceInfo": self.device_info,
            "health": self.last_health,
            "pendingEvents": self.local_state.outbox_size(),
            "version": VERSION,
        }

    def beat(self) -> bool:
        try:
            self.last_health = self.health_monitor.check()
            self.state_manager.set_errors(self.last_health.get("critical", []))
        except Exception:
            log.exception("Contrôle de santé impossible")
        try:
            self.client.heartbeat(self.payload())
        except CentralError as exc:
            if self._ok is not False:
                log.warning("Battement non reçu par le Central : %s", exc)
            self._ok = False
            self.state_manager.mark_contact(False)
            return False
        if self._ok is False:
            log.info("Connexion au Central rétablie")
        self._ok = True
        self.state_manager.mark_contact(True)
        # Renouvelle le bail de l'opération en cours (si le backend le gère), pour ne pas
        # être repris à tort pendant une exécution longue.
        operation_id = self.state_manager.current_operation
        if operation_id and hasattr(self.client, "renew_lease"):
            self.client.renew_lease(operation_id)
        return True

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.beat()
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()

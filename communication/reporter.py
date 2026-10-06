"""Envoi des événements et des bilans au Central.

Chaque envoi porte un `eventId` unique et un numéro `seq` croissant : le Central peut
ignorer les doublons et remettre les événements dans l'ordre. Si le Central est
injoignable, l'envoi est mis en file d'attente (stockage local) et repart dans l'ordre
dès que la connexion revient.
"""

from __future__ import annotations

from typing import Any

from communication.client import CentralError
from utils.helpers import new_id, utc_now_iso
from utils.logger import get_logger

log = get_logger("reporter")


class EventType:
    OPERATION_STARTED = "OPERATION_STARTED"  # l'opération est prise en charge
    USSD_STARTED = "USSD_STARTED"  # le code USSD est parti
    USSD_RESPONSE = "USSD_RESPONSE"  # réponse de l'opérateur lue à l'écran
    SMS_RECEIVED = "SMS_RECEIVED"  # SMS reçu (pendant ou après une opération)
    CATALOG_DISCOVERED = "CATALOG_DISCOVERED"  # exploration des menus : écrans et offres (pass) trouvés
    OPERATION_FINISHED = "OPERATION_FINISHED"  # exécution terminée, observations transmises
    OPERATION_FAILED = "OPERATION_FAILED"  # exécution impossible : rien n'est parti chez l'opérateur
    OPERATION_INTERRUPTED = "OPERATION_INTERRUPTED"  # coupure pendant l'exécution : état inconnu


class Reporter:
    def __init__(self, client, local_state, extractor_id: str, state_manager=None):
        self.client = client
        self.local_state = local_state
        self.extractor_id = extractor_id
        self.state_manager = state_manager

    def emit(self, event_type: str, operation_id: str | None = None,
             payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "eventId": new_id(),
            "extractorId": self.extractor_id,
            "operationId": operation_id,
            "type": event_type,
            "seq": self.local_state.next_seq(),
            "occurredAt": utc_now_iso(),
            "payload": payload or {},
        }
        self.local_state.update(last_event={k: event[k] for k in ("eventId", "type", "operationId", "seq", "occurredAt")})
        log.info("Événement %s%s", event_type, f" ({operation_id})" if operation_id else "")
        self._deliver({"kind": "event", "body": event})
        return event

    def send_result(self, operation_id: str, result: dict[str, Any]) -> None:
        body = {"resultId": new_id(), "extractorId": self.extractor_id, "operationId": operation_id,
                "sentAt": utc_now_iso(), **result}
        self._deliver({"kind": "result", "operationId": operation_id, "body": body})

    def _deliver(self, item: dict[str, Any]) -> None:
        if self.local_state.outbox_size():
            self.local_state.outbox_push(item)
            self.flush()
            return
        try:
            self._send(item)
        except CentralError as exc:
            if exc.retryable:
                log.warning("Central injoignable, envoi mis en attente : %s", exc)
                self.local_state.outbox_push(item)
            else:
                log.error("Envoi refusé par le Central, abandonné : %s", exc)

    def _send(self, item: dict[str, Any]) -> None:
        try:
            if item["kind"] == "event":
                self.client.post_event(item["body"])
            else:
                self.client.post_result(item["operationId"], item["body"])
        except CentralError as exc:
            if self.state_manager is not None and exc.retryable:
                self.state_manager.mark_contact(False)
            raise
        if self.state_manager is not None:
            self.state_manager.mark_contact(True)

    def flush(self) -> bool:
        """Envoie la file d'attente dans l'ordre. Vrai si elle est vide à la fin."""
        while True:
            item = self.local_state.outbox_peek()
            if item is None:
                return True
            try:
                self._send(item)
            except CentralError as exc:
                if exc.retryable:
                    return False
                log.error("Envoi en attente refusé par le Central, abandonné : %s", exc)
            self.local_state.outbox_pop()

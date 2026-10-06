"""Surveille l'arrivée de nouveaux SMS.

Termux:API n'envoie pas de notification à l'arrivée d'un SMS : on relit régulièrement la
boîte de réception et on retient le plus grand `_id` déjà vu (curseur persistant). Au
redémarrage, les SMS arrivés pendant la coupure sont donc bien rapportés.
"""

from __future__ import annotations

from typing import Any

from sms.parser import _norm, parse_sms
from sms.reader import SmsReadError
from utils.logger import get_logger

log = get_logger("sms.listener")


class SmsListener:
    def __init__(self, reader, local_state, config):
        self.reader = reader
        self.local_state = local_state
        self.config = config
        self.last_error: str | None = None

    def prime(self) -> None:
        """Premier démarrage : on ignore l'historique, seuls les nouveaux SMS comptent."""
        if self.local_state.get("sms_cursor") is not None:
            return
        try:
            messages = self.reader.list_recent(self.config.sms_list_limit)
        except SmsReadError as exc:
            self.last_error = str(exc)
            log.warning("Lecture des SMS impossible au démarrage : %s", exc)
            return
        cursor = max((sms.id for sms in messages), default=0)
        self.local_state.update(sms_cursor=cursor)
        log.info("Curseur SMS initialisé à %s (historique ignoré)", cursor)

    def is_monitored(self, sms: dict[str, Any], senders: list[str]) -> bool:
        if not senders:
            return True
        wanted = {_norm(s) for s in senders}
        return _norm(sms["address"]) in wanted or _norm(sms["sender"]) in wanted

    def poll(self) -> list[dict[str, Any]]:
        """Nouveaux SMS (analysés) depuis le dernier appel, filtrés sur les expéditeurs suivis."""
        cursor = self.local_state.get("sms_cursor")
        if cursor is None:
            self.prime()
            return []
        try:
            messages = self.reader.list_recent(self.config.sms_list_limit)
        except SmsReadError as exc:
            if self.last_error != str(exc):
                log.warning("Lecture des SMS impossible : %s", exc)
            self.last_error = str(exc)
            return []
        self.last_error = None

        fresh = [sms for sms in messages if sms.id > cursor]
        if not fresh:
            return []
        if len(fresh) == len(messages) == self.config.sms_list_limit:
            log.warning("Plus de %d SMS depuis la dernière lecture : certains ont pu être manqués "
                        "(augmenter SMS_LIST_LIMIT)", self.config.sms_list_limit)
        self.local_state.update(sms_cursor=max(sms.id for sms in fresh))

        monitored = self.config.monitored_senders()
        parsed = [parse_sms(sms, self.config.operator_sms_senders) for sms in fresh]
        kept = [sms for sms in parsed if self.is_monitored(sms, monitored)]
        if len(kept) < len(parsed):
            log.debug("%d SMS ignoré(s) (expéditeur non suivi)", len(parsed) - len(kept))
        return kept

"""Exécution d'une opération : préparer, lancer l'USSD, collecter les SMS, rapporter.

OPERATION
   -> OPERATION_STARTED
   -> préparation du code (contrôles, secrets locaux)
   -> USSD_STARTED, USSD_RESPONSE...
   -> SMS_RECEIVED... (fenêtre d'attente)
   -> OPERATION_FINISHED  (ou OPERATION_FAILED si rien n'a pu partir)
   -> bilan (POST result)

L'Extracteur ne décide jamais que le transfert a réussi. Le bilan dit seulement :
- EXECUTED       : le code est parti, voici tout ce qui a été observé ;
- NOT_EXECUTED   : rien n'a été envoyé à l'opérateur (opération invalide, expirée...) ;
- INTERRUPTED    : coupure pendant l'exécution, on ne sait pas si le code est parti.
C'est le Balanceur qui confronte ces observations à l'opération et fixe le statut final.

Une opération déjà traitée n'est jamais relancée (risque de double transfert) : si le
Central la renvoie, on lui renvoie le bilan déjà établi.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from communication.client import CentralError
from communication.reporter import EventType
from core.operation import OPERATION_ID_RE, Operation, OperationError
from ussd.launcher import UssdLaunchError
from ussd.session import UssdSession
from utils.helpers import utc_now_iso
from utils.logger import get_logger

log = get_logger("executor")

# Phases enregistrées localement, dans l'ordre. Elles servent à la reprise après coupure.
PHASE_RECEIVED = "RECEIVED"  # rien n'est encore parti
PHASE_USSD_LAUNCHING = "USSD_LAUNCHING"  # lancement en cours : le code est peut-être parti
PHASE_USSD_SENT = "USSD_SENT"  # le code est parti
PHASE_WAITING_SMS = "WAITING_SMS"  # session USSD terminée, attente des SMS

OUTCOME_EXECUTED = "EXECUTED"
OUTCOME_NOT_EXECUTED = "NOT_EXECUTED"
OUTCOME_INTERRUPTED = "INTERRUPTED"


class Executor:
    def __init__(self, config, launcher, sms_listener, reporter, state_manager, local_state,
                 stop_event: threading.Event | None = None):
        self.config = config
        self.launcher = launcher
        self.sms_listener = sms_listener
        self.reporter = reporter
        self.state = state_manager
        self.local_state = local_state
        self.stop_event = stop_event or threading.Event()

    # --- Point d'entrée ----------------------------------------------------------

    def run(self, raw: Any) -> dict[str, Any] | None:
        try:
            operation = Operation.from_dict(raw)
        except OperationError as exc:
            return self._reject_invalid(raw, exc)

        operation_id = operation.operation_id
        previous = self.local_state.executed_result(operation_id)
        if previous is not None:
            log.warning("%s déjà traitée : pas de nouvelle exécution, renvoi du bilan", operation_id)
            self.reporter.send_result(operation_id, previous)
            return previous

        log.info("Opération %s reçue (%s, %s)", operation_id, operation.operator, operation.type)
        self.state.begin_operation(operation_id)
        self.local_state.set_current({**operation.summary(), "phase": PHASE_RECEIVED, "phaseAt": time.time()})
        result: dict[str, Any] | None = None
        try:
            result = self._execute(operation)
        except Exception as exc:  # bug ou panne inattendue : on rapporte plutôt que de mourir
            log.exception("Erreur inattendue pendant %s", operation_id)
            current = self.local_state.get("current_operation") or {}
            result = self._interrupted(operation_id, current.get("phase", PHASE_RECEIVED), f"erreur interne : {exc}")
        finally:
            self.local_state.mark_executed(operation_id, result or {"outcome": OUTCOME_INTERRUPTED})
            self.state.end_operation()
        return result

    # --- Déroulé -----------------------------------------------------------------

    def _execute(self, operation: Operation) -> dict[str, Any]:
        operation_id = operation.operation_id
        started_at = utc_now_iso()
        self.reporter.emit(EventType.OPERATION_STARTED, operation_id,
                           {"operation": operation.summary(), "ussdBackend": self.launcher.name})

        if operation.is_expired():
            return self._not_executed(operation_id, "EXPIRED", "opération expirée avant exécution", started_at)
        try:
            plan = operation.dial_plan(self.config.secrets)
        except OperationError as exc:
            return self._not_executed(operation_id, exc.code, exc.message, started_at)
        if plan.steps and not self.launcher.supports_interaction:
            return self._not_executed(
                operation_id, "INTERACTION_NOT_SUPPORTED",
                f"le mécanisme USSD « {self.launcher.name} » ne sait pas naviguer dans un menu : "
                "utiliser un code en une fois ou USSD_BACKEND=http", started_at)

        # SMS arrivés avant le lancement : rapportés, mais pas rattachés à cette opération.
        self.report_idle_sms()

        timeout = operation.ussd_timeout or self.config.ussd_timeout
        sim_slot = operation.sim_slot if operation.sim_slot is not None else self.config.ussd_sim_slot
        session = UssdSession(self.launcher, timeout, sim_slot)

        def on_started() -> None:
            self.local_state.set_phase(PHASE_USSD_SENT)
            self.reporter.emit(EventType.USSD_STARTED, operation_id, {
                "code": plan.code.redacted,
                "steps": [step.redacted for step in plan.steps],
                "backend": self.launcher.name,
                "simSlot": sim_slot,
                "responseCaptured": self.launcher.captures_response,
            })

        def on_response(index: int, parsed: dict[str, Any], reply) -> None:
            self.reporter.emit(EventType.USSD_RESPONSE, operation_id,
                               {"index": index, "sessionStatus": reply.status, "error": reply.error, "response": parsed})

        self.local_state.set_phase(PHASE_USSD_LAUNCHING)
        log.info("Lancement USSD %s", plan.code.redacted)
        try:
            ussd = session.run(plan.code, plan.steps, on_started, on_response)
        except UssdLaunchError as exc:
            return self._not_executed(operation_id, "USSD_LAUNCH_FAILED", str(exc), started_at)
        log.info("Session USSD terminée : %s", ussd["status"])

        self.local_state.set_phase(PHASE_WAITING_SMS)
        sms = self._collect_sms(operation)

        result = {
            "outcome": OUTCOME_EXECUTED,
            "startedAt": started_at,
            "finishedAt": utc_now_iso(),
            "ussd": ussd,
            "sms": sms,
        }
        self.reporter.emit(EventType.OPERATION_FINISHED, operation_id, result)
        self.reporter.send_result(operation_id, result)
        return result

    def _collect_sms(self, operation: Operation) -> dict[str, Any]:
        wait = operation.sms_wait_seconds
        wait = self.config.sms_wait_seconds if wait is None else wait
        expected = operation.sms_expected_count
        senders = self.config.senders_for(operation.operator)
        begin = time.monotonic()
        deadline = begin + wait
        received: list[int] = []
        matching = 0
        interrupted = False

        log.info("Attente des SMS de confirmation (%d attendu(s), %.0f s max)", expected, wait)
        while True:
            for sms in self.sms_listener.poll():
                self.reporter.emit(EventType.SMS_RECEIVED, operation.operation_id,
                                   {"attribution": "DURING_OPERATION", "sms": sms})
                received.append(sms["smsId"])
                if self.sms_listener.is_monitored(sms, senders):
                    matching += 1
            if matching >= expected:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self.stop_event.wait(min(self.config.sms_poll_interval, remaining)):
                interrupted = True
                break

        return {
            "receivedIds": received,
            "fromOperatorSenders": matching,
            "expected": expected,
            "waitedSeconds": round(time.monotonic() - begin, 1),
            "windowClosedEarly": matching >= expected,
            "interrupted": interrupted,
        }

    # --- Bilans sans exécution ---------------------------------------------------

    def _not_executed(self, operation_id: str, reason: str, message: str, started_at: str | None = None) -> dict[str, Any]:
        log.warning("%s non exécutée : %s (%s)", operation_id, reason, message)
        result = {
            "outcome": OUTCOME_NOT_EXECUTED,
            "reason": reason,
            "message": message,
            "ussdSent": False,
            "startedAt": started_at,
            "finishedAt": utc_now_iso(),
        }
        self.reporter.emit(EventType.OPERATION_FAILED, operation_id, result)
        self.reporter.send_result(operation_id, result)
        return result

    def _interrupted(self, operation_id: str, phase: str, message: str) -> dict[str, Any]:
        if phase == PHASE_RECEIVED:
            return self._not_executed(operation_id, "INTERRUPTED_BEFORE_USSD", message)
        result = {
            "outcome": OUTCOME_INTERRUPTED,
            "phase": phase,
            "ussdSent": True if phase in (PHASE_USSD_SENT, PHASE_WAITING_SMS) else None,
            "message": message,
            "warning": "Ne pas relancer sans vérifier : le code USSD a pu être exécuté par l'opérateur.",
            "finishedAt": utc_now_iso(),
        }
        log.error("%s interrompue en phase %s : état inconnu", operation_id, phase)
        self.reporter.emit(EventType.OPERATION_INTERRUPTED, operation_id, result)
        self.reporter.send_result(operation_id, result)
        return result

    def _reject_invalid(self, raw: Any, exc: OperationError) -> None:
        operation_id = str(raw.get("operationId") or "") if isinstance(raw, dict) else ""
        log.error("Opération rejetée : %s", exc.message)
        if not OPERATION_ID_RE.match(operation_id):
            self.reporter.emit(EventType.OPERATION_FAILED, None,
                               {"outcome": OUTCOME_NOT_EXECUTED, "reason": exc.code, "message": exc.message})
            return None
        if self.local_state.executed_result(operation_id) is None:
            result = self._not_executed(operation_id, exc.code, exc.message)
            self.local_state.mark_executed(operation_id, result)
        return None

    # --- SMS hors opération et reprise -------------------------------------------

    def report_idle_sms(self) -> None:
        """Rapporte les SMS arrivés hors opération, rattachés à la dernière si elle est récente."""
        messages = self.sms_listener.poll()
        if not messages:
            return
        last = self.local_state.get("last_operation") or {}
        recent = last and time.time() - last.get("finishedAt", 0) <= self.config.late_sms_grace
        for sms in messages:
            self.reporter.emit(EventType.SMS_RECEIVED, last.get("operationId") if recent else None,
                               {"attribution": "AFTER_OPERATION" if recent else "NONE", "sms": sms})

    def recover_interrupted(self) -> None:
        """Au démarrage : une opération était-elle en cours au moment de la coupure ?"""
        current = self.local_state.get("current_operation")
        if not current:
            return
        operation_id = current.get("operationId")
        phase = current.get("phase", PHASE_RECEIVED)
        log.warning("Redémarrage : %s était en cours (phase %s)", operation_id, phase)
        try:
            known = self.reporter.client.operation_status(operation_id)
            log.info("État de %s connu du Central : %s", operation_id, known)
        except CentralError as exc:
            log.warning("État de %s non disponible auprès du Central : %s", operation_id, exc)
        result = self._interrupted(operation_id, phase, "redémarrage de l'Extracteur pendant l'exécution")
        self.local_state.mark_executed(operation_id, result)

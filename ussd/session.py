"""Session USSD : lancement du code, saisies successives, suivi de l'état.

STARTED -> WAITING_RESPONSE -> INTERACTION_REQUIRED -> ... -> COMPLETED
                                                       \\-> TIMEOUT / FAILED

C'est ce module qui changera le plus si le mécanisme d'exécution évolue
(thl_ussd_service, service d'accessibilité...) : le reste de l'Extracteur ne voit que
`UssdSession.run()` et son résultat.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable

from core.operation import DialInput
from ussd.launcher import BaseLauncher, UssdReply
from ussd.parser import parse_ussd_response
from utils.helpers import utc_now_iso
from utils.logger import get_logger

log = get_logger("ussd.session")


class SessionState(str, Enum):
    STARTED = "STARTED"
    WAITING_RESPONSE = "WAITING_RESPONSE"
    INTERACTION_REQUIRED = "INTERACTION_REQUIRED"
    COMPLETED = "COMPLETED"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"


TERMINAL = (SessionState.COMPLETED, SessionState.TIMEOUT, SessionState.FAILED)

# on_started()                         : le code est parti
# on_response(index, parsed, reply)    : une réponse de l'opérateur a été lue
StartedCallback = Callable[[], None]
ResponseCallback = Callable[[int, dict[str, Any], UssdReply], None]


class UssdSession:
    def __init__(self, launcher: BaseLauncher, timeout: float, sim_slot: int | None = None):
        self.launcher = launcher
        self.timeout = timeout
        self.sim_slot = sim_slot
        self.state = SessionState.STARTED
        self.transcript: list[dict[str, Any]] = []
        self.error: str | None = None
        self.cancelled = False
        self.steps_sent = 0
        self._responses = 0

    def run(self, code: DialInput, steps: list[DialInput],
            on_started: StartedCallback | None = None,
            on_response: ResponseCallback | None = None) -> dict[str, Any]:
        """Exécute la session. Lève UssdLaunchError si le code n'a pas pu partir."""
        self.state = SessionState.STARTED
        self._record("out", code.redacted)
        reply = self.launcher.start(code.value, self.sim_slot, self.timeout)  # UssdLaunchError remonte
        self.state = SessionState.WAITING_RESPONSE
        if on_started:
            on_started()
        self._apply(reply, on_response)

        for step in steps:
            if self.state != SessionState.INTERACTION_REQUIRED:
                break
            self._record("out", step.redacted)
            self.state = SessionState.WAITING_RESPONSE
            try:
                reply = self.launcher.reply(step.value, self.timeout)
            except Exception as exc:  # passerelle défaillante : on garde une trace, pas de crash
                log.exception("Saisie USSD impossible")
                reply = UssdReply(status=SessionState.FAILED.value, error=str(exc))
            self.steps_sent += 1
            self._apply(reply, on_response)

        if self.state == SessionState.INTERACTION_REQUIRED:
            # Le menu attend encore une saisie que l'opération ne prévoit pas : on ferme.
            self.cancelled = True
            self.launcher.cancel()
        elif self.state == SessionState.WAITING_RESPONSE and not self.launcher.captures_response:
            # Mécanisme sans capture (termux) : le code est parti, la suite viendra par SMS.
            self.state = SessionState.COMPLETED

        return self.result(len(steps))

    def _apply(self, reply: UssdReply, on_response: ResponseCallback | None) -> None:
        try:
            self.state = SessionState(reply.status)
        except ValueError:
            self.state = SessionState.FAILED
            reply.error = reply.error or f"statut inconnu : {reply.status}"
        if reply.error:
            self.error = reply.error
        if reply.captured and reply.message is not None:
            parsed = parse_ussd_response(reply.message)
            self._record("in", reply.message, status=self.state.value)
            if on_response:
                on_response(self._responses, parsed, reply)
            self._responses += 1

    def _record(self, direction: str, text: str, **extra: Any) -> None:
        self.transcript.append({"direction": direction, "text": text, "at": utc_now_iso(), **extra})

    def result(self, steps_total: int) -> dict[str, Any]:
        return {
            "status": self.state.value,
            "backend": self.launcher.name,
            "responseCaptured": self.launcher.captures_response,
            "responses": self._responses,
            "stepsSent": self.steps_sent,
            "stepsTotal": steps_total,
            "cancelled": self.cancelled,
            "error": self.error,
            "transcript": self.transcript,
        }

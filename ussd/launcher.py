"""Mécanismes de lancement USSD disponibles.

- `termux` : `termux-telephony-call` (Termux:API). Lance le code via le composeur Android.
  Limites : la réponse de l'opérateur s'affiche dans une fenêtre système et n'est pas
  capturée, pas de navigation dans un menu, et Termux:API ne signale pas un refus de
  permission. On compose donc des codes « en une fois » (`*144*1*numéro*montant*PIN#`)
  et la confirmation vient des SMS.
- `http` : passerelle locale (application Android compagnon utilisant
  `TelephonyManager.sendUssdRequest` ou un service d'accessibilité, ex. thl_ussd_service).
  Capture les réponses et gère les menus. Contrat décrit dans docs/PROTOCOLE.md.
- `mock` : réponses scriptées, pour développer et tester sans SIM.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from utils.helpers import run_command
from utils.logger import get_logger

log = get_logger("ussd.launcher")

# Statuts renvoyés par un lanceur (repris par la session USSD).
WAITING_RESPONSE = "WAITING_RESPONSE"
INTERACTION_REQUIRED = "INTERACTION_REQUIRED"
COMPLETED = "COMPLETED"
TIMEOUT = "TIMEOUT"
FAILED = "FAILED"


class UssdLaunchError(Exception):
    """Le lancement a échoué : rien n'a été envoyé à l'opérateur."""


@dataclass
class UssdReply:
    status: str
    message: str | None = None
    captured: bool = True
    error: str | None = None


class BaseLauncher:
    name = "base"
    captures_response = True
    supports_interaction = True

    def start(self, code: str, sim_slot: int | None, timeout: float) -> UssdReply:
        raise NotImplementedError

    def reply(self, value: str, timeout: float) -> UssdReply:
        raise NotImplementedError

    def cancel(self) -> None:
        """Ferme la session en cours si elle attend encore une saisie."""


class TermuxCallLauncher(BaseLauncher):
    name = "termux"
    captures_response = False
    supports_interaction = False

    def __init__(self, command_timeout: float):
        self.command_timeout = command_timeout

    def start(self, code: str, sim_slot: int | None, timeout: float) -> UssdReply:
        if sim_slot is not None:
            log.warning("termux-telephony-call ne choisit pas la SIM : simSlot=%s ignoré (SIM par défaut)", sim_slot)
        # Termux:API remplace lui-même « # » par « %23 » : on passe le code tel quel.
        result = run_command(["termux-telephony-call", code], self.command_timeout)
        output = f"{result.stdout} {result.stderr}".strip()
        if result.timed_out:
            raise UssdLaunchError("termux-telephony-call ne répond pas (application Termux:API installée ?)")
        if not result.ok or any(word in output.lower() for word in ("error", "exception", "denied")):
            raise UssdLaunchError(f"termux-telephony-call a échoué (code {result.returncode}) : {output[:200]}")
        return UssdReply(status=WAITING_RESPONSE, captured=False)


class HttpBridgeLauncher(BaseLauncher):
    name = "http"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session_id: str | None = None

    def _post(self, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read() or b"{}")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _to_reply(data: dict[str, Any]) -> UssdReply:
        status = str(data.get("status") or "").upper()
        mapping = {
            "WAITING_INPUT": INTERACTION_REQUIRED,
            "INTERACTION_REQUIRED": INTERACTION_REQUIRED,
            "COMPLETED": COMPLETED,
            "TIMEOUT": TIMEOUT,
        }
        return UssdReply(
            status=mapping.get(status, FAILED),
            message=data.get("message"),
            error=data.get("error") or (None if status in mapping else f"statut passerelle : {status or 'vide'}"),
        )

    def start(self, code: str, sim_slot: int | None, timeout: float) -> UssdReply:
        self.session_id = None
        try:
            data = self._post("/ussd/start", {"code": code, "simSlot": sim_slot}, timeout)
        except (TimeoutError, socket.timeout):
            # La demande est peut-être partie : on ne peut pas dire « rien n'a été envoyé ».
            return UssdReply(status=TIMEOUT, error="pas de réponse de la passerelle USSD")
        except urllib.error.HTTPError as exc:
            raise UssdLaunchError(f"passerelle USSD : HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise UssdLaunchError(f"passerelle USSD injoignable : {exc}") from exc
        self.session_id = data.get("sessionId")
        return self._to_reply(data)

    def reply(self, value: str, timeout: float) -> UssdReply:
        try:
            return self._to_reply(self._post("/ussd/reply", {"sessionId": self.session_id, "input": value}, timeout))
        except (TimeoutError, socket.timeout):
            return UssdReply(status=TIMEOUT, error="pas de réponse de la passerelle USSD")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return UssdReply(status=FAILED, error=f"passerelle USSD : {exc}")

    def cancel(self) -> None:
        if not self.session_id:
            return
        try:
            self._post("/ussd/cancel", {"sessionId": self.session_id}, 5)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("Annulation de la session USSD impossible : %s", exc)


class MockLauncher(BaseLauncher):
    """Réponses scriptées. Script JSON : liste de {"status", "message", "sms"?}.

    Une entrée avec "sms": {"sender", "body"} simule le SMS de confirmation de l'opérateur.
    """

    name = "mock"

    def __init__(self, script: list[dict[str, Any]] | None = None,
                 sms_sink: Callable[[str, str], None] | None = None, fail_on_start: bool = False):
        self.script = list(script or [])
        self.sms_sink = sms_sink
        self.fail_on_start = fail_on_start
        self.calls: list[tuple[str, str]] = []
        self._cursor = 0

    @classmethod
    def from_file(cls, path: Path | None, sms_sink: Callable[[str, str], None] | None = None) -> "MockLauncher":
        script = None
        if path is not None:
            script = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(script, dict):
                script = script.get("responses", [])
        return cls(script, sms_sink)

    def _next(self) -> UssdReply:
        if self._cursor < len(self.script):
            entry = self.script[self._cursor]
            self._cursor += 1
        else:
            entry = {"status": COMPLETED, "message": "Mock : demande prise en compte."}
        if entry.get("sms") and self.sms_sink:
            self.sms_sink(entry["sms"].get("sender", "MOCK"), entry["sms"].get("body", ""))
        return UssdReply(status=entry.get("status", COMPLETED), message=entry.get("message"), error=entry.get("error"))

    def start(self, code: str, sim_slot: int | None, timeout: float) -> UssdReply:
        self.calls.append(("start", code))
        if self.fail_on_start:
            raise UssdLaunchError("mock : échec de lancement simulé")
        self._cursor = 0
        return self._next()

    def reply(self, value: str, timeout: float) -> UssdReply:
        self.calls.append(("reply", value))
        return self._next()

    def cancel(self) -> None:
        self.calls.append(("cancel", ""))


class TreeMockLauncher(BaseLauncher):
    """Menus USSD simulés, pour tester l'exploration sans SIM.

    `tree` : chemin (touches jointes par « / », racine = "") -> {"message", "status"?}.
    `status` par défaut : INTERACTION_REQUIRED si le nœud a des enfants, sinon COMPLETED.
    """

    name = "tree-mock"

    def __init__(self, tree: dict[str, dict[str, Any]]):
        self.tree = tree
        self.calls: list[tuple[str, str]] = []
        self._path: list[str] = []

    @classmethod
    def from_file(cls, path: Path) -> "TreeMockLauncher":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data.get("tree", data))

    def _node(self) -> dict[str, Any] | None:
        return self.tree.get("/".join(self._path))

    def _has_children(self) -> bool:
        prefix = ("/".join(self._path) + "/") if self._path else ""
        return any(key.startswith(prefix) and key != "/".join(self._path) for key in self.tree)

    def _reply(self) -> UssdReply:
        node = self._node()
        if node is None:
            return UssdReply(status=FAILED, error="chemin inexistant dans l'arbre simulé")
        status = node.get("status") or (INTERACTION_REQUIRED if self._has_children() else COMPLETED)
        return UssdReply(status=status, message=node.get("message", ""))

    def start(self, code: str, sim_slot: int | None, timeout: float) -> UssdReply:
        self.calls.append(("start", code))
        self._path = []
        return self._reply()

    def reply(self, value: str, timeout: float) -> UssdReply:
        self.calls.append(("reply", value))
        self._path.append(value)
        return self._reply()

    def cancel(self) -> None:
        self.calls.append(("cancel", ""))
        self._path = []


def create_launcher(config, sms_sink: Callable[[str, str], None] | None = None) -> BaseLauncher:
    if config.ussd_backend == "termux":
        return TermuxCallLauncher(config.termux_cmd_timeout)
    if config.ussd_backend == "http":
        return HttpBridgeLauncher(config.ussd_bridge_url)
    return MockLauncher.from_file(config.mock_ussd_script, sms_sink)

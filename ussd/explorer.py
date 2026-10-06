"""Exploration des menus USSD d'un opérateur, pour en sortir le catalogue (pass et prix).

L'explorateur parcourt l'arbre des menus en profondeur et rapporte, pour chaque écran,
le texte, les options et les offres détectées. Il ne fait **que lire**.

Sécurité — l'explorateur est incapable de réaliser un achat, par construction :
- il n'envoie que des touches d'options lues dans un menu (chiffres, * ou #), jamais une
  saisie libre ;
- il s'arrête à tout écran qui demande une saisie (`asksForInput`) : il ne tape jamais de
  PIN, de montant ni de numéro ;
- il ne sélectionne jamais une option dont le libellé ressemble à une confirmation d'achat
  ou à un retour (liste `EXPLORE_BLOCK_LABELS` : confirmer, valider, payer, oui, retour...).

Les sessions USSD étant courtes et sans « retour » fiable, chaque chemin est rejoué depuis
la racine : pour visiter [1, 3], on relance le code, puis on envoie 1, puis 3.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from core.operation import DialInput
from ussd.launcher import BaseLauncher, UssdLaunchError
from ussd.parser import parse_ussd_response
from ussd.session import SessionState
from utils.helpers import utc_now_iso
from utils.logger import get_logger

log = get_logger("ussd.explorer")

SAFE_KEY_RE = re.compile(r"^[0-9*#]{1,4}$")

# Libellés d'options que l'explorateur ne sélectionne jamais : confirmation d'achat et
# navigation « retour ». Comparés en mots entiers (pour ne pas bloquer « validité » dans un
# pass à cause de « valider »).
DEFAULT_BLOCK_LABELS = (
    "confirmer", "confirmation", "valider", "payer", "paiement", "oui", "accepter", "ok",
    "retour", "precedent", "précédent", "annuler", "quitter", "menu principal", "accueil",
)


@dataclass
class ExploreNode:
    path: list[str]
    keys_label: list[str]
    text: str
    status: str
    is_menu: bool
    asks_for_input: bool
    options: list[dict[str, str]]
    offers: list[dict[str, Any]]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "pathLabels": self.keys_label,
            "depth": len(self.path),
            "status": self.status,
            "isMenu": self.is_menu,
            "asksForInput": self.asks_for_input,
            "text": self.text,
            "options": self.options,
            "offers": self.offers,
            "error": self.error,
        }


@dataclass
class ExploreResult:
    rootCode: str
    nodes: list[dict[str, Any]] = field(default_factory=list)
    catalog: list[dict[str, Any]] = field(default_factory=list)
    pathsVisited: int = 0
    truncated: bool = False
    stoppedReason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rootCode": self.rootCode,
            "exploredAt": utc_now_iso(),
            "pathsVisited": self.pathsVisited,
            "nodes": self.nodes,
            "catalog": self.catalog,
            "truncated": self.truncated,
            "stoppedReason": self.stoppedReason,
        }


class UssdExplorer:
    def __init__(self, launcher: BaseLauncher, config, stop_event=None):
        self.launcher = launcher
        self.config = config
        self.stop_event = stop_event
        self.timeout = config.ussd_timeout
        self.max_depth = config.explore_max_depth
        self.max_nodes = config.explore_max_nodes
        self.step_delay = config.explore_step_delay
        labels = [b.lower() for b in (config.explore_block_labels or DEFAULT_BLOCK_LABELS)]
        self._block_re = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(label) for label in labels))

    def _blocked(self, label: str) -> bool:
        return bool(self._block_re.search(label.lower()))

    def _should_stop(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def explore(self, root_code: DialInput,
                on_node: Callable[[ExploreNode], None] | None = None) -> ExploreResult:
        if not self.launcher.supports_interaction:
            result = ExploreResult(rootCode=root_code.redacted)
            result.stoppedReason = (
                f"le mécanisme USSD « {self.launcher.name} » ne lit pas les réponses : "
                "exploration impossible (utiliser USSD_BACKEND=http)")
            log.error(result.stoppedReason)
            return result

        result = ExploreResult(rootCode=root_code.redacted)
        self._root_code = root_code.value
        seen_signatures: set[str] = set()
        queue: list[tuple[list[str], list[str]]] = [([], [])]  # (touches, libellés)

        while queue:
            if self._should_stop():
                result.stoppedReason = "arrêt demandé"
                break
            if len(result.nodes) >= self.max_nodes:
                result.truncated = True
                result.stoppedReason = f"limite de {self.max_nodes} écrans atteinte"
                break

            path, labels = queue.pop(0)
            node = self._visit(path, labels)
            if node is None:
                continue
            result.pathsVisited += 1
            result.nodes.append(node.to_dict())
            for offer in node.offers:
                result.catalog.append({**offer, "path": path, "pathLabels": labels})
            if on_node:
                on_node(node)

            if not node.is_menu or node.asks_for_input or len(path) >= self.max_depth:
                continue
            # Éviter de réexplorer un menu identique atteint par un autre chemin.
            signature = "|".join(sorted(opt["label"] for opt in node.options))
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)

            for opt in node.options:
                key, label = opt["key"], opt["label"]
                if not SAFE_KEY_RE.match(key):
                    continue
                if self._blocked(label):
                    log.debug("Option non suivie (libellé bloqué) : %s. %s", key, label)
                    continue
                queue.append((path + [key], labels + [label]))

        self.launcher.cancel()
        log.info("Exploration terminée : %d écran(s), %d offre(s)%s",
                 len(result.nodes), len(result.catalog), " (tronquée)" if result.truncated else "")
        return result

    def _visit(self, path: list[str], labels: list[str]) -> ExploreNode | None:
        """Rejoue le chemin depuis la racine et analyse l'écran atteint."""
        if self.step_delay and path:
            time.sleep(self.step_delay)
        try:
            reply = self.launcher.start(self._root_code, None, self.timeout)
            for key in path:
                if reply.status not in (SessionState.INTERACTION_REQUIRED.value,):
                    # Le chemin n'est plus disponible (session fermée) : on abandonne ce chemin.
                    self.launcher.cancel()
                    return None
                reply = self.launcher.reply(key, self.timeout)
        except UssdLaunchError as exc:
            log.warning("Chemin %s inaccessible : %s", "/".join(path) or "(racine)", exc)
            return ExploreNode(path, labels, "", SessionState.FAILED.value, False, False, [], [], error=str(exc))

        parsed = parse_ussd_response(reply.message)
        node = ExploreNode(
            path=path,
            keys_label=labels,
            text=parsed["text"],
            status=reply.status,
            is_menu=parsed["isMenu"],
            asks_for_input=parsed["asksForInput"],
            options=parsed["options"],
            offers=parsed["offers"],
            error=reply.error,
        )
        # On a l'écran voulu ; on ferme la session avant de rejouer le chemin suivant.
        self.launcher.cancel()
        return node

    _root_code: str = ""  # valeur réelle du code racine, fixée au début de explore()

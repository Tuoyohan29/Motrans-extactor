"""Petits outils partagés : horloge, identifiants, commandes Termux, extraction de texte."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def new_id() -> str:
    return uuid.uuid4().hex


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_command(args: list[str], timeout: float) -> CommandResult:
    """Lance une commande sans shell (aucune injection possible par les arguments)."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return CommandResult(127, "", f"commande introuvable : {args[0]}")
    except subprocess.TimeoutExpired as exc:
        return CommandResult(-1, _text(exc.stdout), _text(exc.stderr), timed_out=True)
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)


def run_json_command(args: list[str], timeout: float) -> Any:
    """Lance une commande Termux:API qui répond en JSON. Lève RuntimeError en cas d'échec."""
    result = run_command(args, timeout)
    if result.timed_out:
        raise RuntimeError(f"{args[0]} : pas de réponse après {timeout:.0f} s (Termux:API installé ?)")
    if not result.ok:
        raise RuntimeError(f"{args[0]} : code {result.returncode} {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{args[0]} : réponse illisible {result.stdout[:200]!r}") from exc
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"{args[0]} : {data['error']}")
    return data


# --- Extraction d'informations dans un texte opérateur (USSD ou SMS) ----------------
# Ce ne sont que des indices : l'Extracteur ne conclut jamais sur le résultat.

_AMOUNT_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[ .  ]\d{3})+|\d+)(?:[.,](\d{1,2}))?"
    r"\s*(?:F\s?CFA|FCFA|XOF|CFA|F)(?![A-Za-z])",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"(?<![\d.,])(?:\+|00)?\d{8,13}(?![\d.,]?\d)")
_REFERENCE_RE = re.compile(
    r"(?:r[ée]f(?:[ée]rence)?|trans(?:action)?[\s_-]*id|id[\s_-]*(?:de[\s_-]*(?:la[\s_-]*)?)?"
    r"trans(?:action)?|txn[\s_-]*id)\s*[:#.\-]?\s*([A-Z0-9][A-Z0-9.\-_/]{3,}[A-Z0-9])",
    re.IGNORECASE,
)
_BALANCE_LABEL_RE = re.compile(r"solde", re.IGNORECASE)
_FEE_LABEL_RE = re.compile(r"frais", re.IGNORECASE)


@dataclass
class AmountMatch:
    value: float | int
    start: int
    end: int
    text: str


def _to_number(integer_part: str, decimals: str | None) -> float | int:
    digits = re.sub(r"[ .  ]", "", integer_part)
    if decimals and int(decimals):
        return float(f"{digits}.{decimals}")
    return int(digits)


def find_amounts(text: str) -> list[AmountMatch]:
    return [
        AmountMatch(_to_number(m.group(1), m.group(2)), m.start(), m.end(), m.group(0).strip())
        for m in _AMOUNT_RE.finditer(text or "")
    ]


def _labelled_amount(text: str, label_re: re.Pattern, amounts: list[AmountMatch]) -> AmountMatch | None:
    for label in label_re.finditer(text):
        for amount in amounts:
            if 0 <= amount.start - label.end() <= 30:
                return amount
    return None


def extract_money_info(text: str) -> dict[str, Any]:
    """Montants trouvés dans le texte, avec frais et solde quand ils sont libellés."""
    amounts = find_amounts(text)
    balance = _labelled_amount(text or "", _BALANCE_LABEL_RE, amounts)
    fee = _labelled_amount(text or "", _FEE_LABEL_RE, amounts)
    main = next((a for a in amounts if a is not balance and a is not fee), None)
    return {
        "amount": main.value if main else None,
        "amounts": [a.value for a in amounts],
        "fee": fee.value if fee else None,
        "balance": balance.value if balance else None,
    }


def extract_phone_numbers(text: str) -> list[str]:
    seen: list[str] = []
    for match in _PHONE_RE.finditer(text or ""):
        if match.group(0) not in seen:
            seen.append(match.group(0))
    return seen


def extract_references(text: str) -> list[str]:
    seen: list[str] = []
    for match in _REFERENCE_RE.finditer(text or ""):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen

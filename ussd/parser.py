"""Transforme une réponse USSD brute en informations exploitables par le Balanceur.

Le parseur décrit ce qu'il voit (menu, montants, numéros, références) ; il ne dit pas
si l'opération a réussi.
"""

from __future__ import annotations

import re
from typing import Any

from utils.helpers import extract_money_info, extract_phone_numbers, extract_references

_OPTION_RE = re.compile(r"^\s*(\d{1,2}|[*#0]{1,2}|9{2})\s*[.):\-]\s*(.+?)\s*$")
_INPUT_HINT_RE = re.compile(r"(saisi|entre[rz]|tape[rz]|code\s+secret|pin|montant|num[ée]ro)", re.IGNORECASE)


def parse_ussd_response(text: str | None) -> dict[str, Any]:
    text = (text or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    options = []
    for line in lines:
        match = _OPTION_RE.match(line)
        if match:
            options.append({"key": match.group(1), "label": match.group(2)})
    is_menu = len(options) >= 2
    return {
        "text": text,
        "lines": lines,
        "isMenu": is_menu,
        "options": options if is_menu else [],
        "asksForInput": bool(not is_menu and _INPUT_HINT_RE.search(text)),
        **extract_money_info(text),
        "phoneNumbers": extract_phone_numbers(text),
        "references": extract_references(text),
    }

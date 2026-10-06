"""Transforme une réponse USSD brute en informations exploitables par le Balanceur.

Le parseur décrit ce qu'il voit (menu, options, montants, numéros, références, offres) ;
il ne dit jamais si une opération a réussi.
"""

from __future__ import annotations

import re
from typing import Any

from utils.helpers import extract_money_info, extract_phone_numbers, extract_references, find_amounts

_OPTION_RE = re.compile(r"^\s*(\d{1,2}|[*#0]{1,2}|9{2})\s*[.):\-]\s*(.+?)\s*$")
_INPUT_HINT_RE = re.compile(r"(saisi|entre[rz]|tape[rz]|r[ée]pond|code\s+secret|pin|montant|num[ée]ro)", re.IGNORECASE)

# Volume de données : 500 Mo, 1,5 Go, 2GB...
_VOLUME_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(Go|Mo|Ko|GB|MB|KB|G|M)\b", re.IGNORECASE)
# Validité : 7 jours, 24h, 1 mois, 1 semaine, illimité...
_VALIDITY_RE = re.compile(
    r"(\d+)\s*(jours?|j|heures?|h|min(?:utes?)?|mois|semaines?|sem|ans?)\b"
    r"|(\b(?:illimit[ée]e?s?|permanent|non[\s-]?stop)\b)",
    re.IGNORECASE,
)
_VALIDITY_UNITS = {
    "j": "jours", "jour": "jours", "jours": "jours",
    "h": "heures", "heure": "heures", "heures": "heures",
    "min": "minutes", "minute": "minutes", "minutes": "minutes",
    "sem": "semaines", "semaine": "semaines", "semaines": "semaines",
    "mois": "mois", "an": "ans", "ans": "ans",
}


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
        "offers": extract_offers(text, options if is_menu else []),
        **extract_money_info(text),
        "phoneNumbers": extract_phone_numbers(text),
        "references": extract_references(text),
    }


def _volume(text: str) -> tuple[float | int | None, str | None]:
    match = _VOLUME_RE.search(text)
    if not match:
        return None, None
    raw = match.group(1).replace(",", ".")
    value: float | int = float(raw)
    if value.is_integer():
        value = int(value)
    unit = match.group(2).upper()
    unit = {"G": "GO", "M": "MO", "GB": "GO", "MB": "MO", "KB": "KO"}.get(unit, unit)
    return value, unit.capitalize()


_VALIDITY_HINT_RE = re.compile(r"valab|valid", re.IGNORECASE)


def _validity(text: str) -> tuple[int | None, str | None]:
    # Un pass peut contenir deux durées (ex. « 20 min ... validité 24h ») : on privilégie
    # celle qui suit « valable / validité », sinon la dernière durée du texte.
    hint = _VALIDITY_HINT_RE.search(text)
    matches = list(_VALIDITY_RE.finditer(text))
    if not matches:
        return None, None
    match = next((m for m in matches if hint and m.start() >= hint.end()), matches[-1])
    if match.group(3):  # illimité / permanent
        return None, "illimité"
    unit = _VALIDITY_UNITS.get(match.group(2).lower(), match.group(2).lower())
    return int(match.group(1)), unit


def _offer_from_text(text: str, option_key: str | None = None) -> dict[str, Any] | None:
    """Un pass si la ligne porte un volume de données, ou un prix associé à une durée."""
    volume, volume_unit = _volume(text)
    validity, validity_unit = _validity(text)
    amounts = find_amounts(text)
    price = amounts[0].value if amounts else None
    if volume is None and not (price is not None and (validity is not None or validity_unit)):
        return None
    name = text
    if option_key:
        name = _OPTION_RE.sub(lambda m: m.group(2), text, count=1)
    return {
        "optionKey": option_key,
        "name": name.strip(),
        "raw": text.strip(),
        "price": price,
        "priceCurrency": "XOF" if price is not None else None,
        "volume": volume,
        "volumeUnit": volume_unit,
        "validity": validity,
        "validityUnit": validity_unit,
    }


def extract_offers(text: str, options: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """Offres (« pass ») trouvées dans le texte.

    Une option de menu décrivant un pass est rattachée à sa touche (pour pouvoir la
    sélectionner plus tard) ; sinon on analyse chaque ligne du texte libre.
    """
    option_labels = {opt["label"] for opt in (options or [])}
    offers: list[dict[str, Any]] = []
    for opt in options or []:
        offer = _offer_from_text(opt["label"], opt["key"])
        if offer:
            offers.append(offer)
    for line in (text or "").splitlines():
        line = line.strip()
        match = _OPTION_RE.match(line)
        if match and match.group(2) in option_labels:
            continue  # déjà traité via les options
        offer = _offer_from_text(line)
        if offer:
            offers.append(offer)
    return offers

"""Identité du téléphone : appareil, Android, Termux, SIM et opérateur."""

from __future__ import annotations

import os
import platform
from typing import Any

from config import VERSION
from utils.helpers import command_exists, run_command, run_json_command
from utils.logger import get_logger

log = get_logger("device.info")


def _getprop(name: str) -> str | None:
    if not command_exists("getprop"):
        return None
    result = run_command(["getprop", name], 5)
    return result.stdout.strip() or None if result.ok else None


def telephony_info(timeout: float) -> dict[str, Any]:
    """Sortie de termux-telephony-deviceinfo (dict vide si indisponible)."""
    if not command_exists("termux-telephony-deviceinfo"):
        return {}
    try:
        data = run_json_command(["termux-telephony-deviceinfo"], timeout)
    except RuntimeError as exc:
        log.warning("Infos téléphonie indisponibles : %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


# Codes opérateurs (MCC+MNC) connus -> opérateur normalisé. Côte d'Ivoire notamment.
_OPERATOR_CODES = {
    "61201": "orange", "61202": "orange",
    "61203": "moov",
    "61205": "mtn",
}
_KNOWN_OPERATORS = ("orange", "mtn", "moov")


def detect_operator(device_info: dict[str, Any]) -> str | None:
    """Opérateur déduit de la SIM (code puis nom), en minuscules, ou None si inconnu."""
    code = (device_info.get("simOperatorCode") or "").strip()
    if code in _OPERATOR_CODES:
        return _OPERATOR_CODES[code]
    name = (device_info.get("simOperator") or device_info.get("networkOperator") or "").lower()
    for known in _KNOWN_OPERATORS:
        if known in name:
            return known
    return None


def sim_operator_warning(device_info: dict[str, Any], operators: list[str]) -> str | None:
    """Alerte si la SIM détectée ne correspond à aucun opérateur servi (sinon None).

    Évite le piège « tirer un code Orange sur une SIM MTN » : on compare l'opérateur
    déduit de la SIM aux opérateurs déclarés dans EXTRACTOR_OPERATORS.
    """
    detected = detect_operator(device_info)
    if not detected or not operators:
        return None  # SIM inconnue ou pas d'opérateur configuré : on ne peut pas trancher
    if detected in [o.strip().lower() for o in operators]:
        return None
    label = device_info.get("simOperator") or device_info.get("networkOperator") or detected
    return (f"SIM détectée « {label} » (opérateur {detected}) mais EXTRACTOR_OPERATORS = "
            f"{', '.join(operators)}. Risque de tirer un code du mauvais opérateur : "
            f"vérifie EXTRACTOR_OPERATORS, la SIM, ou USSD_SIM_SLOT.")


def collect_device_info(config, install_id: str) -> dict[str, Any]:
    telephony = telephony_info(config.termux_cmd_timeout) if config.needs_termux else {}
    return {
        "deviceId": install_id,
        "model": _getprop("ro.product.model"),
        "manufacturer": _getprop("ro.product.manufacturer"),
        "androidVersion": _getprop("ro.build.version.release"),
        "termuxVersion": os.environ.get("TERMUX_VERSION"),
        "pythonVersion": platform.python_version(),
        "extractorVersion": VERSION,
        "ussdBackend": config.ussd_backend,
        "smsBackend": config.sms_backend,
        "simOperator": telephony.get("sim_operator_name"),
        "simOperatorCode": telephony.get("sim_operator"),
        "networkOperator": telephony.get("network_operator_name"),
        "simCountry": telephony.get("sim_country_iso"),
        "phoneCount": telephony.get("phone_count"),
    }

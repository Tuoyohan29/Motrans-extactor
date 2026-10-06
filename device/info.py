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

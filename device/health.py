"""Santé du téléphone : réseau, SIM, Termux:API, batterie, connexion au Central.

`critical` : problèmes qui empêchent d'exécuter (l'Extracteur passe en ERROR et ne prend
plus d'opération). `warnings` : à surveiller, sans bloquer.
"""

from __future__ import annotations

import time
from typing import Any

from device.info import telephony_info
from utils.helpers import command_exists, run_json_command


class HealthMonitor:
    def __init__(self, config, state_manager, sms_listener=None):
        self.config = config
        self.state_manager = state_manager
        self.sms_listener = sms_listener

    def check(self) -> dict[str, Any]:
        critical: list[str] = []
        warnings: list[str] = []
        checks: dict[str, Any] = {}

        if self.config.needs_termux:
            self._check_termux(checks, critical, warnings)

        if self.sms_listener is not None and self.sms_listener.last_error:
            warnings.append(f"Lecture des SMS impossible : {self.sms_listener.last_error}")
        checks["sms"] = {"backend": self.config.sms_backend,
                         "ok": not (self.sms_listener and self.sms_listener.last_error)}

        last_contact = self.state_manager.last_contact
        checks["central"] = {"lastContactSecondsAgo": round(time.time() - last_contact) if last_contact else None}

        return {"ok": not critical, "critical": critical, "warnings": warnings, "checks": checks}

    def _check_termux(self, checks: dict[str, Any], critical: list[str], warnings: list[str]) -> None:
        timeout = self.config.termux_cmd_timeout
        if not command_exists("termux-battery-status"):
            critical.append("Paquet termux-api absent (pkg install termux-api)")
            checks["termuxApi"] = {"ok": False}
            return
        try:
            battery = run_json_command(["termux-battery-status"], timeout)
        except RuntimeError as exc:
            critical.append(f"Termux:API ne répond pas (application installée ?) : {exc}")
            checks["termuxApi"] = {"ok": False}
            return
        checks["termuxApi"] = {"ok": True}

        percentage = battery.get("percentage") if isinstance(battery, dict) else None
        plugged = isinstance(battery, dict) and str(battery.get("plugged", "")).upper() not in ("", "UNPLUGGED")
        checks["battery"] = {"percentage": percentage, "plugged": plugged,
                             "temperature": battery.get("temperature") if isinstance(battery, dict) else None}
        if isinstance(percentage, (int, float)) and percentage < self.config.battery_min and not plugged:
            warnings.append(f"Batterie faible ({percentage} %) et téléphone débranché")

        telephony = telephony_info(timeout)
        sim_state = telephony.get("sim_state")
        checks["sim"] = {"state": sim_state, "operator": telephony.get("sim_operator_name")}
        checks["network"] = {
            "operator": telephony.get("network_operator_name"),
            "type": telephony.get("network_type"),
            "dataState": telephony.get("data_state"),
            "roaming": telephony.get("network_roaming"),
        }
        if self.config.real_device:
            if sim_state and sim_state != "ready":
                critical.append(f"SIM non prête (état : {sim_state})")
            if not telephony.get("network_operator_name"):
                warnings.append("Aucun réseau mobile détecté")

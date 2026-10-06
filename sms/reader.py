"""Lecture des SMS reçus.

- `termux` : `termux-sms-list` (Termux:API, permission SMS accordée à Termux:API).
- `file`   : fichier JSON lines pour développer sans téléphone (une ligne = un SMS).
- `none`   : pas de lecture de SMS.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.helpers import run_json_command, utc_now_iso


class SmsReadError(Exception):
    pass


@dataclass
class RawSms:
    id: int
    address: str
    sender: str
    body: str
    received: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class TermuxSmsReader:
    name = "termux"

    def __init__(self, command_timeout: float):
        self.command_timeout = command_timeout

    def list_recent(self, limit: int) -> list[RawSms]:
        try:
            data = run_json_command(["termux-sms-list", "-l", str(limit), "-t", "inbox"], self.command_timeout)
        except RuntimeError as exc:
            raise SmsReadError(str(exc)) from exc
        if not isinstance(data, list):
            raise SmsReadError(f"termux-sms-list : liste attendue, reçu {type(data).__name__}")
        messages = []
        for item in data:
            if not isinstance(item, dict) or "_id" not in item:
                continue
            address = str(item.get("address") or item.get("number") or "")
            messages.append(RawSms(
                id=int(item["_id"]),
                address=address,
                sender=str(item.get("sender") or address),
                body=str(item.get("body") or ""),
                received=str(item.get("received") or ""),
                raw=item,
            ))
        return sorted(messages, key=lambda sms: sms.id)


class FileSmsReader:
    """Fichier JSON lines : {"sender": "OrangeMoney", "body": "...", "received": "..."}."""

    name = "file"

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def add(self, sender: str, body: str) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"sender": sender, "body": body, "received": utc_now_iso()}, ensure_ascii=False) + "\n")

    def list_recent(self, limit: int) -> list[RawSms]:
        with self._lock:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
        messages = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            sender = str(item.get("sender") or "")
            messages.append(RawSms(index, str(item.get("address") or sender), sender,
                                   str(item.get("body") or ""), str(item.get("received") or ""), item))
        return messages[-limit:]


class NullSmsReader:
    name = "none"

    def list_recent(self, limit: int) -> list[RawSms]:
        return []


def create_reader(config):
    if config.sms_backend == "termux":
        return TermuxSmsReader(config.termux_cmd_timeout)
    if config.sms_backend == "file":
        return FileSmsReader(config.sms_mock_file)
    return NullSmsReader()

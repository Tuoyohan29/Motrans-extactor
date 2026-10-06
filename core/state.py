"""État local de l'Extracteur : IDLE, BUSY, ERROR ou OFFLINE."""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Any


class ExtractorStatus(str, Enum):
    IDLE = "IDLE"  # prêt, attend une opération
    BUSY = "BUSY"  # exécute une opération
    ERROR = "ERROR"  # problème local bloquant (SIM absente, Termux:API muet...)
    OFFLINE = "OFFLINE"  # Central injoignable


class StateManager:
    def __init__(self, local_state=None):
        self._lock = threading.Lock()
        self._local_state = local_state
        self._current_operation: str | None = None
        self._errors: list[str] = []
        self._connected = False
        self._last_contact: float | None = None
        self._last_status: ExtractorStatus | None = None

    @property
    def status(self) -> ExtractorStatus:
        with self._lock:
            return self._compute()

    def _compute(self) -> ExtractorStatus:
        if self._current_operation:
            return ExtractorStatus.BUSY
        if self._errors:
            return ExtractorStatus.ERROR
        if not self._connected:
            return ExtractorStatus.OFFLINE
        return ExtractorStatus.IDLE

    def _changed(self) -> None:
        status = self._compute()
        if status != self._last_status:
            self._last_status = status
            if self._local_state is not None:
                self._local_state.update(local_status=status.value)

    @property
    def current_operation(self) -> str | None:
        with self._lock:
            return self._current_operation

    @property
    def errors(self) -> list[str]:
        with self._lock:
            return list(self._errors)

    @property
    def last_contact(self) -> float | None:
        with self._lock:
            return self._last_contact

    def can_accept_operation(self) -> bool:
        return self.status == ExtractorStatus.IDLE

    def begin_operation(self, operation_id: str) -> None:
        with self._lock:
            self._current_operation = operation_id
            self._changed()

    def end_operation(self) -> None:
        with self._lock:
            self._current_operation = None
            self._changed()

    def set_errors(self, errors: list[str]) -> None:
        with self._lock:
            self._errors = list(errors)
            self._changed()

    def mark_contact(self, ok: bool) -> None:
        with self._lock:
            self._connected = ok
            if ok:
                self._last_contact = time.time()
            self._changed()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._compute().value,
                "currentOperation": self._current_operation,
                "errors": list(self._errors),
            }

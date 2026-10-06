"""Doublures partagées par les tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from communication.client import CentralError
from communication.reporter import Reporter
from config import Config
from core.executor import Executor
from core.state import StateManager
from sms.listener import SmsListener
from sms.reader import FileSmsReader
from storage.local_state import LocalState
from ussd.launcher import MockLauncher


class FakeClient:
    def __init__(self):
        self.events: list[dict] = []
        self.results: list[tuple[str, dict]] = []
        self.offline = False

    def _check(self):
        if self.offline:
            raise CentralError("hors ligne")

    def post_event(self, event):
        self._check()
        self.events.append(event)

    def post_result(self, operation_id, result):
        self._check()
        self.results.append((operation_id, result))

    def operation_status(self, operation_id):
        self._check()
        return {"operationId": operation_id, "status": "UNKNOWN"}

    def types(self):
        return [event["type"] for event in self.events]


class Harness:
    def __init__(self, script=None, launcher=None, **config_overrides):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        values = dict(
            central_url="http://central.test", extractor_token="t", extractor_id="EXT-T",
            ussd_backend="mock", sms_backend="file", sms_mock_file=root / "sms.jsonl",
            sms_wait_seconds=0.3, sms_poll_interval=0.05, data_dir=root,
            operator_sms_senders={"orange": ["OrangeMoney"]}, secrets={"pin": "4321"},
        )
        values.update(config_overrides)
        self.config = Config(**values)
        self.local_state = LocalState(root / "state.json")
        self.state = StateManager(self.local_state)
        self.client = FakeClient()
        self.reporter = Reporter(self.client, self.local_state, "EXT-T", self.state)
        self.reader = FileSmsReader(self.config.sms_mock_file)
        self.listener = SmsListener(self.reader, self.local_state, self.config)
        self.listener.prime()
        self.launcher = launcher or MockLauncher(script, self.reader.add)
        self.executor = self.make_executor()

    def make_executor(self):
        return Executor(self.config, self.launcher, self.listener, self.reporter, self.state, self.local_state)

    def close(self):
        self.tmp.cleanup()


def transfer(**overrides):
    operation = {
        "operationId": "OP-458",
        "operator": "orange",
        "type": "transfer",
        "ussdCode": "*144*1*{beneficiary}*{amount}*{secret.pin}#",
        "beneficiary": "0707070707",
        "amount": 500,
        "parameters": {},
    }
    operation.update(overrides)
    return operation

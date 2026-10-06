import unittest

from communication.reporter import EventType
from core.executor import PHASE_USSD_SENT
from ussd.launcher import MockLauncher, TermuxCallLauncher
from tests.helpers import Harness, transfer

CONFIRMATION = {"sender": "OrangeMoney", "body": "Transfert de 500 FCFA vers 0707070707 effectue. Ref: PP1234.AB"}


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.h = None

    def tearDown(self):
        if self.h:
            self.h.close()

    def test_full_flow_reports_observations_without_deciding(self):
        self.h = Harness([{"status": "COMPLETED", "message": "Demande en cours", "sms": CONFIRMATION}])
        result = self.h.executor.run(transfer())

        self.assertEqual(self.h.client.types(), [
            EventType.OPERATION_STARTED, EventType.USSD_STARTED, EventType.USSD_RESPONSE,
            EventType.SMS_RECEIVED, EventType.OPERATION_FINISHED])
        self.assertEqual(result["outcome"], "EXECUTED")
        self.assertNotIn("success", str(result).lower())
        self.assertEqual(result["sms"]["fromOperatorSenders"], 1)
        self.assertTrue(result["sms"]["windowClosedEarly"])
        self.assertEqual(self.h.client.results[0][0], "OP-458")
        self.assertEqual(self.h.state.status.value, "IDLE")  # les envois réussis valent contact
        self.assertIsNone(self.h.state.current_operation)

    def test_pin_never_leaves_the_phone(self):
        self.h = Harness([{"status": "COMPLETED", "message": "ok"}])
        self.h.executor.run(transfer())
        self.assertEqual(self.h.launcher.calls[0], ("start", "*144*1*0707070707*500*4321#"))
        self.assertNotIn("4321", str(self.h.client.events) + str(self.h.client.results))
        self.assertNotIn("4321", self.h.local_state.path.read_text())

    def test_menu_navigation_with_steps(self):
        self.h = Harness([
            {"status": "INTERACTION_REQUIRED", "message": "1. Transfert\n2. Solde"},
            {"status": "INTERACTION_REQUIRED", "message": "Entrez le numero"},
            {"status": "COMPLETED", "message": "Transfert de 500 FCFA en cours"},
        ])
        result = self.h.executor.run(transfer(ussdCode="*144#", parameters={"steps": ["1", "{beneficiary}"]}))
        self.assertEqual([c[1] for c in self.h.launcher.calls], ["*144#", "1", "0707070707"])
        self.assertEqual(result["ussd"]["status"], "COMPLETED")
        self.assertEqual(self.h.client.types().count(EventType.USSD_RESPONSE), 3)

    def test_session_left_waiting_is_cancelled(self):
        self.h = Harness([{"status": "INTERACTION_REQUIRED", "message": "1. A\n2. B"}])
        result = self.h.executor.run(transfer(ussdCode="*144#"))
        self.assertTrue(result["ussd"]["cancelled"])
        self.assertEqual(self.h.launcher.calls[-1][0], "cancel")

    def test_never_executes_twice(self):
        self.h = Harness([{"status": "COMPLETED", "message": "ok"}])
        self.h.executor.run(transfer())
        self.h.executor.run(transfer())
        self.assertEqual(sum(1 for c in self.h.launcher.calls if c[0] == "start"), 1)
        self.assertEqual(len(self.h.client.results), 2)  # le bilan est renvoyé, pas l'opération

    def test_launch_failure_is_not_executed(self):
        self.h = Harness(launcher=MockLauncher(fail_on_start=True))
        result = self.h.executor.run(transfer())
        self.assertEqual(result["outcome"], "NOT_EXECUTED")
        self.assertEqual(result["reason"], "USSD_LAUNCH_FAILED")
        self.assertEqual(self.h.client.types()[-1], EventType.OPERATION_FAILED)

    def test_termux_backend_refuses_menu_steps(self):
        self.h = Harness(launcher=TermuxCallLauncher(1))
        result = self.h.executor.run(transfer(ussdCode="*144#", parameters={"steps": ["1"]}))
        self.assertEqual(result["reason"], "INTERACTION_NOT_SUPPORTED")

    def test_expired_and_invalid_operations(self):
        self.h = Harness()
        self.assertEqual(self.h.executor.run(transfer(expiresAt="2000-01-01T00:00:00Z"))["reason"], "EXPIRED")
        self.h.executor.run({"operationId": "OP-9", "operator": "orange"})
        self.assertEqual(self.h.client.results[-1][1]["reason"], "INVALID_OPERATION")
        self.assertEqual(self.h.launcher.calls, [])

    def test_offline_events_are_queued_then_flushed_in_order(self):
        self.h = Harness([{"status": "COMPLETED", "message": "ok", "sms": CONFIRMATION}])
        self.h.client.offline = True
        self.h.executor.run(transfer())
        self.assertEqual(self.h.client.events, [])
        self.assertEqual(self.h.local_state.outbox_size(), 6)  # 5 événements + 1 bilan

        self.h.client.offline = False
        self.assertTrue(self.h.reporter.flush())
        seqs = [e["seq"] for e in self.h.client.events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(self.h.client.results), 1)

    def test_recovery_after_crash_reports_interrupted_without_relaunch(self):
        self.h = Harness()
        self.h.local_state.set_current({"operationId": "OP-77", "phase": PHASE_USSD_SENT})
        self.h.executor.recover_interrupted()
        self.assertEqual(self.h.client.types(), [EventType.OPERATION_INTERRUPTED])
        self.assertEqual(self.h.client.results[0][1]["outcome"], "INTERRUPTED")
        self.assertTrue(self.h.client.results[0][1]["ussdSent"])
        self.assertIsNone(self.h.local_state.get("current_operation"))

        # Le Central renvoie OP-77 : elle n'est pas relancée.
        self.h.executor.run(transfer(operationId="OP-77"))
        self.assertEqual(self.h.launcher.calls, [])

    def test_late_sms_attached_to_last_operation(self):
        self.h = Harness([{"status": "COMPLETED", "message": "ok"}])
        self.h.executor.run(transfer(parameters={"smsWaitSeconds": 0}))
        self.h.reader.add("OrangeMoney", "Transfert de 500 FCFA effectue")
        self.h.reader.add("+2250101010101", "Salut")  # expéditeur non suivi : ignoré
        self.h.executor.report_idle_sms()
        late = self.h.client.events[-1]
        self.assertEqual(late["type"], EventType.SMS_RECEIVED)
        self.assertEqual(late["operationId"], "OP-458")
        self.assertEqual(late["payload"]["attribution"], "AFTER_OPERATION")
        self.assertEqual(self.h.client.types().count(EventType.SMS_RECEIVED), 1)


if __name__ == "__main__":
    unittest.main()

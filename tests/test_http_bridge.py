"""Tests de bout en bout du backend USSD « http » contre une fausse passerelle qui parle
le meme contrat que l'application Android (tools/fake_bridge.py)."""

import unittest

from config import Config
from core.operation import DialInput, Operation
from tests.helpers import Harness, transfer
from tools.fake_bridge import BridgeServer
from ussd.explorer import UssdExplorer
from ussd.launcher import HttpBridgeLauncher
from ussd.session import SessionState, UssdSession

MENU_TREE = {
    "": {"message": "Orange Money\n1. Transfert\n2. Achat pass\n3. Solde"},
    "1": {"message": "Entrez le numero", "status": "COMPLETED"},
    "2": {"message": "Pass\n1. 1 Go - 1000 FCFA - 7 jours\n2. 5 Go - 3000 FCFA - 30 jours"},
    "2/1": {"message": "Pass 1 Go a 1000 FCFA valable 7 jours en cours.", "status": "COMPLETED"},
    "2/2": {"message": "Pass 5 Go a 3000 FCFA valable 30 jours en cours.", "status": "COMPLETED"},
    "3": {"message": "Votre solde est de 12 350 FCFA.", "status": "COMPLETED"},
}


class HttpBridgeTest(unittest.TestCase):
    def setUp(self):
        self.bridge = BridgeServer(MENU_TREE).start_background()

    def tearDown(self):
        self.bridge.stop()

    def _launcher(self):
        return HttpBridgeLauncher(self.bridge.url)

    def test_health_reachable(self):
        import urllib.request
        with urllib.request.urlopen(f"{self.bridge.url}/health", timeout=5) as response:
            self.assertEqual(response.status, 200)

    def test_single_shot_captures_response(self):
        session = UssdSession(self._launcher(), timeout=5)
        result = session.run(DialInput("*155#", "*155#"), [])
        # Racine = menu (a des enfants) -> la session attend une saisie non fournie -> annulee.
        self.assertTrue(result["responseCaptured"])
        self.assertEqual(result["transcript"][1]["direction"], "in")
        self.assertIn("Transfert", result["transcript"][1]["text"])

    def test_menu_navigation_reaches_terminal_screen(self):
        session = UssdSession(self._launcher(), timeout=5)
        result = session.run(DialInput("*144#", "*144#"), [DialInput("2", "2"), DialInput("1", "1")])
        self.assertEqual(result["status"], SessionState.COMPLETED.value)
        self.assertEqual(result["stepsSent"], 2)
        sent = [t["text"] for t in result["transcript"] if t["direction"] == "out"]
        self.assertEqual(sent, ["*144#", "2", "1"])
        self.assertIn("1 Go", result["transcript"][-1]["text"])

    def test_executor_over_http_backend(self):
        harness = Harness(launcher=self._launcher())
        try:
            result = harness.executor.run(transfer(
                ussdCode="*144#",
                parameters={"steps": ["2", "1"], "smsExpectedCount": 0, "smsWaitSeconds": 0},
            ))
            self.assertEqual(result["outcome"], "EXECUTED")
            self.assertEqual(result["ussd"]["status"], "COMPLETED")
            self.assertEqual(result["ussd"]["backend"], "http")
            self.assertTrue(result["ussd"]["responseCaptured"])
            types = harness.client.types()
            self.assertIn("USSD_RESPONSE", types)
            self.assertEqual(harness.client.results[0][0], "OP-458")
        finally:
            harness.close()

    def test_exploration_over_http_backend(self):
        config = Config(central_url="http://c", extractor_token="t", explore_step_delay=0.0, ussd_timeout=5)
        plan = Operation.from_dict({
            "operationId": "E", "operator": "orange", "type": "explore", "ussdCode": "*144#",
        }).dial_plan({})
        result = UssdExplorer(self._launcher(), config).explore(plan.code).to_dict()
        prices = {offer["price"] for offer in result["catalog"]}
        self.assertIn(1000, prices)
        self.assertIn(3000, prices)
        # Le solde (ecran terminal, sans offre) a bien ete visite aussi.
        self.assertTrue(any(node["path"] == ["3"] for node in result["nodes"]))

    def test_unreachable_bridge_is_launch_error(self):
        from ussd.launcher import UssdLaunchError
        launcher = HttpBridgeLauncher("http://127.0.0.1:1")  # port ferme
        session = UssdSession(launcher, timeout=2)
        with self.assertRaises(UssdLaunchError):
            session.run(DialInput("*144#", "*144#"), [])


if __name__ == "__main__":
    unittest.main()

import json
import unittest
from pathlib import Path

from communication.reporter import EventType
from config import Config
from core.operation import Operation
from ussd.explorer import UssdExplorer
from ussd.launcher import MockLauncher, TreeMockLauncher
from tests.helpers import Harness

TREE = json.loads((Path(__file__).resolve().parent.parent / "tools" / "mock_ussd_tree.json").read_text())["tree"]


def explore_config(**over):
    values = dict(central_url="http://c", extractor_token="t", explore_step_delay=0.0, **over)
    return Config(**values)


class ExplorerTest(unittest.TestCase):
    def _run(self, tree=None, **cfg):
        launcher = TreeMockLauncher(tree or TREE)
        explorer = UssdExplorer(launcher, explore_config(**cfg))
        plan = Operation.from_dict({"operationId": "E", "operator": "orange",
                                    "type": "explore", "ussdCode": "*144#"}).dial_plan({})
        return launcher, explorer.explore(plan.code)

    def test_discovers_full_catalog(self):
        _, result = self._run()
        names = {(o["volume"], o["volumeUnit"], o["price"], o["validity"]) for o in result.catalog}
        self.assertIn((150, "Mo", 300, 24), names)
        self.assertIn((1, "Go", 1000, 7), names)
        self.assertIn((5, "Go", 3000, 30), names)
        # Les pass d'appels (prix + durée, sans volume) sont aussi reconnus.
        self.assertTrue(any(o["price"] == 250 and o["validity"] == 24 for o in result.catalog))

    def test_never_confirms_a_purchase(self):
        _, result = self._run()
        # L'écran de confirmation est atteint (il décrit l'offre),
        confirm_visited = any(n["path"] == ["2", "1", "1"] for n in result.nodes)
        self.assertTrue(confirm_visited)
        # mais on ne descend jamais plus bas : "Confirmer" et "Annuler" sont bloqués.
        self.assertFalse(any(n["path"][:3] == ["2", "1", "1"] and len(n["path"]) > 3 for n in result.nodes))

    def test_stops_at_input_prompt(self):
        tree = {"": {"message": "1. Acheter\n2. Solde"},
                "1": {"message": "Entrez le montant", "status": "INTERACTION_REQUIRED"}}
        launcher, result = self._run(tree)
        self.assertTrue(any(n["path"] == ["1"] and n["asksForInput"] for n in result.nodes))
        # Aucune saisie n'est envoyée après l'invite.
        self.assertEqual([c for c in launcher.calls if c[0] == "reply"].count(("reply", "1")), 1)

    def test_respects_block_labels(self):
        tree = {"": {"message": "1. Pass\n2. Payer une facture"},
                "1": {"message": "1 Go - 1000 FCFA - 7 jours", "status": "COMPLETED"},
                "2": {"message": "Facture\n1. Confirmer"}}
        _, result = self._run(tree)
        self.assertFalse(any(n["path"] == ["2"] for n in result.nodes))  # "Payer" bloqué

    def test_node_limit_truncates(self):
        _, result = self._run(explore_max_nodes=2)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.nodes), 2)

    def test_termux_backend_cannot_explore(self):
        from ussd.launcher import TermuxCallLauncher
        explorer = UssdExplorer(TermuxCallLauncher(1), explore_config())
        plan = Operation.from_dict({"operationId": "E", "operator": "o", "type": "explore",
                                    "ussdCode": "*144#"}).dial_plan({})
        result = explorer.explore(plan.code)
        self.assertEqual(result.nodes, [])
        self.assertIn("exploration impossible", result.stoppedReason)


class ExploreOperationTest(unittest.TestCase):
    def test_executor_runs_exploration_and_reports_catalog(self):
        h = Harness(launcher=TreeMockLauncher(TREE), explore_step_delay=0.0)
        try:
            result = h.executor.run({"operationId": "OP-CAT", "operator": "orange",
                                     "type": "explore", "ussdCode": "*144#"})
            self.assertEqual(result["mode"], "explore")
            self.assertIn(EventType.CATALOG_DISCOVERED, h.client.types())
            catalog = result["explore"]["catalog"]
            self.assertTrue(any(o["volume"] == 1 and o["price"] == 1000 for o in catalog))
            self.assertEqual(h.client.results[0][0], "OP-CAT")
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()

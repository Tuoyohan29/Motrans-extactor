"""Tests de la détection d'opérateur et de l'alerte SIM / EXTRACTOR_OPERATORS."""

import unittest

from device.info import detect_operator, sim_operator_warning


class DetectOperatorTest(unittest.TestCase):
    def test_by_code(self):
        self.assertEqual(detect_operator({"simOperatorCode": "61205"}), "mtn")
        self.assertEqual(detect_operator({"simOperatorCode": "61201"}), "orange")

    def test_by_name(self):
        self.assertEqual(detect_operator({"simOperator": "MTN CI"}), "mtn")
        self.assertEqual(detect_operator({"simOperator": "Orange CI"}), "orange")

    def test_network_name_fallback(self):
        self.assertEqual(detect_operator({"networkOperator": "MOOV Africa"}), "moov")

    def test_unknown(self):
        self.assertIsNone(detect_operator({"simOperator": "Vodafone"}))
        self.assertIsNone(detect_operator({}))


class SimWarningTest(unittest.TestCase):
    def test_mismatch_warns(self):
        warning = sim_operator_warning({"simOperator": "MTN CI", "simOperatorCode": "61205"}, ["orange"])
        self.assertIsNotNone(warning)
        self.assertIn("mtn", warning)
        self.assertIn("EXTRACTOR_OPERATORS", warning)

    def test_match_silent(self):
        self.assertIsNone(sim_operator_warning({"simOperatorCode": "61205"}, ["mtn"]))
        self.assertIsNone(sim_operator_warning({"simOperator": "MTN CI"}, ["orange", "mtn"]))

    def test_unknown_sim_silent(self):
        self.assertIsNone(sim_operator_warning({"simOperator": "Inconnu"}, ["orange"]))

    def test_no_operators_silent(self):
        self.assertIsNone(sim_operator_warning({"simOperatorCode": "61205"}, []))


if __name__ == "__main__":
    unittest.main()

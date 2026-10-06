import unittest

from sms.parser import parse_sms
from sms.reader import RawSms
from ussd.parser import parse_ussd_response


class SmsParserTest(unittest.TestCase):
    def test_orange_transfer(self):
        sms = RawSms(7, "OrangeMoney", "OrangeMoney",
                     "Transfert de 5 000 FCFA vers 0707070707 effectue. Frais: 50 FCFA. "
                     "Nouveau solde: 12 350 FCFA. Ref: PP261006.1452.B47120.", "2026-10-06 10:00:00")
        parsed = parse_sms(sms, {"orange": ["orangemoney"]})
        self.assertEqual(parsed["operator"], "orange")
        self.assertEqual(parsed["amount"], 5000)
        self.assertEqual(parsed["fee"], 50)
        self.assertEqual(parsed["balance"], 12350)
        self.assertEqual(parsed["phoneNumber"], "0707070707")
        self.assertEqual(parsed["reference"], "PP261006.1452.B47120")

    def test_transaction_id_and_international_number(self):
        sms = RawSms(1, "MobileMoney", "MobileMoney",
                     "Vous avez envoye 1500F a +2250505050505. ID de la transaction: 8812345678. "
                     "Solde actuel: 300F.", "")
        parsed = parse_sms(sms)
        self.assertIsNone(parsed["operator"])
        self.assertEqual(parsed["amount"], 1500)
        self.assertEqual(parsed["balance"], 300)
        self.assertEqual(parsed["reference"], "8812345678")
        self.assertIn("+2250505050505", parsed["phoneNumbers"])

    def test_no_money_info(self):
        parsed = parse_sms(RawSms(2, "Orange", "Orange", "Bienvenue sur le reseau", ""))
        self.assertIsNone(parsed["amount"])
        self.assertEqual(parsed["amounts"], [])


class UssdParserTest(unittest.TestCase):
    def test_menu(self):
        parsed = parse_ussd_response("Orange Money\n1. Transfert\n2. Paiement\n3) Solde\n0: Retour")
        self.assertTrue(parsed["isMenu"])
        self.assertEqual([o["key"] for o in parsed["options"]], ["1", "2", "3", "0"])

    def test_input_prompt(self):
        parsed = parse_ussd_response("Entrez le numero du beneficiaire")
        self.assertFalse(parsed["isMenu"])
        self.assertTrue(parsed["asksForInput"])

    def test_confirmation_text(self):
        parsed = parse_ussd_response("Transfert de 500 FCFA vers 0707070707 en cours.")
        self.assertEqual(parsed["amount"], 500)
        self.assertEqual(parsed["phoneNumbers"], ["0707070707"])


if __name__ == "__main__":
    unittest.main()

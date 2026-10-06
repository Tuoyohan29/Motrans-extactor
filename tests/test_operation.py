import unittest

from core.operation import Operation, OperationError
from tests.helpers import transfer


class OperationTest(unittest.TestCase):
    def test_dial_plan_fills_fields_and_masks_secret(self):
        plan = Operation.from_dict(transfer()).dial_plan({"pin": "4321"})
        self.assertEqual(plan.code.value, "*144*1*0707070707*500*4321#")
        self.assertEqual(plan.code.redacted, "*144*1*0707070707*500*****#")

    def test_operator_specific_secret_wins(self):
        plan = Operation.from_dict(transfer()).dial_plan({"pin": "1111", "pin_orange": "2222"})
        self.assertIn("2222", plan.code.value)

    def test_beneficiary_cannot_inject_ussd_characters(self):
        operation = Operation.from_dict(transfer(beneficiary="07*99#"))
        with self.assertRaises(OperationError) as ctx:
            operation.dial_plan({"pin": "4321"})
        self.assertEqual(ctx.exception.code, "INVALID_OPERATION")

    def test_missing_secret(self):
        with self.assertRaises(OperationError) as ctx:
            Operation.from_dict(transfer()).dial_plan({})
        self.assertEqual(ctx.exception.code, "MISSING_SECRET")

    def test_unknown_placeholder(self):
        with self.assertRaises(OperationError) as ctx:
            Operation.from_dict(transfer(ussdCode="*144*{foo}#")).dial_plan({})
        self.assertEqual(ctx.exception.code, "UNKNOWN_PLACEHOLDER")

    def test_code_must_end_with_hash(self):
        with self.assertRaises(OperationError):
            Operation.from_dict(transfer(ussdCode="*144*1")).dial_plan({})

    def test_steps_and_params(self):
        op = Operation.from_dict(transfer(ussdCode="*144#", parameters={
            "steps": ["1", "{beneficiary}", "{amount}", "{param.choice}", "{secret.pin}"], "choice": "2"}))
        plan = op.dial_plan({"pin": "4321"})
        self.assertEqual([s.value for s in plan.steps], ["1", "0707070707", "500", "2", "4321"])
        self.assertEqual(plan.steps[-1].redacted, "****")

    def test_invalid_operation_id(self):
        with self.assertRaises(OperationError):
            Operation.from_dict(transfer(operationId="../../admin"))

    def test_expired(self):
        self.assertTrue(Operation.from_dict(transfer(expiresAt="2000-01-01T00:00:00Z")).is_expired())
        self.assertFalse(Operation.from_dict(transfer()).is_expired())

    def test_decimal_amount_refused_in_code(self):
        with self.assertRaises(OperationError):
            Operation.from_dict(transfer(amount=500.5)).dial_plan({"pin": "4321"})


if __name__ == "__main__":
    unittest.main()

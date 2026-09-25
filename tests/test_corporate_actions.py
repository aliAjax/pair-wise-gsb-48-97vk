import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError
from src.rules import DomainRules


def base_data(**overrides):
    data = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0,
            'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'none', 'action_ratio': 1.0}
    data.update(overrides)
    return data


MERGER_DATA = base_data(corporate_action='merger', action_ratio=2.0)
DIVIDEND_DATA = base_data(corporate_action='dividend', action_ratio=0.5)


class CorporateActionRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_merger_adjusts_quantity_and_price_value_neutral(self):
        prepared = self.rules.prepare_create(MERGER_DATA)
        # 2股旧股换1股新股：数量减半、均价翻倍
        self.assertEqual(prepared["adjusted_quantity"], 500)
        self.assertEqual(prepared["adjusted_price"], 25.0)

    def test_merger_apply_keeps_position_value(self):
        record = {"id": 1, "state": "captured", "payload": self.rules.prepare_create(MERGER_DATA)}
        state, payload, summary = self.rules.apply_action(record, "apply_corporate", {})
        self.assertEqual(state, "adjusted")
        self.assertEqual(payload["effective_quantity"], 500)
        self.assertEqual(payload["effective_price"], 25.0)
        self.assertEqual(payload["effective_amount"], 12500.0)
        comparison = payload["corporate_comparison"]
        self.assertEqual(comparison["before"], {"quantity": 1000, "price": 12.5, "amount": 12500.0})
        self.assertEqual(comparison["after"], {"quantity": 500, "price": 25.0, "amount": 12500.0})

    def test_dividend_is_not_precomputed_at_create(self):
        prepared = self.rules.prepare_create(DIVIDEND_DATA)
        self.assertNotIn("cash_entitlement", prepared)
        self.assertEqual(prepared["adjusted_quantity"], 1000)

    def test_dividend_apply_computes_entitlement_on_effective_shares(self):
        record = {"id": 1, "state": "captured", "payload": self.rules.prepare_create(DIVIDEND_DATA)}
        _, payload, _ = self.rules.apply_action(record, "apply_corporate", {})
        self.assertEqual(payload["effective_quantity"], 1000)
        pending = payload["pending_cash_entitlement"]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["share_quantity"], 1000)
        self.assertEqual(pending["amount"], 500.0)

    def test_settlement_uses_effective_quantity_after_merger(self):
        record = {"id": 1, "state": "approved",
                  "payload": {**self.rules.prepare_create(MERGER_DATA),
                              "corporate_applied": True, "effective_quantity": 500,
                              "effective_price": 25.0, "adjusted_quantity": 500, "adjusted_price": 25.0}}
        with self.assertRaises(ValidationError):
            # 按原数量1000交收必须被拒，应按换股后的有效数量500核对
            self.rules.apply_action(record, "settle", {"delivered_quantity": 1000, "cash_paid": 12518.0})
        _, payload, _ = self.rules.apply_action(record, "settle", {"delivered_quantity": 500, "cash_paid": 12518.0})
        self.assertEqual(payload["settlement_check"]["effective_quantity"], 500)
        self.assertTrue(payload["settlement_check"]["matched"])


class CorporateActionWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _act(self, record, action, role, data=None):
        return self.service.act(Actor("operator", role), record["id"], record["version"], action, data or {})

    def test_merger_full_workflow_settles_on_effective_quantity(self):
        record = self.service.create(Actor("creator", "trader"), "TRD-MRG-1", MERGER_DATA)
        record = self._act(record, "apply_corporate", "corporate_actions")
        self.assertEqual(record["payload"]["effective_quantity"], 500)
        record = self._act(record, "approve", "settlement_officer")
        with self.assertRaises(ValidationError):
            self._act(record, "settle", "settlement_officer",
                      {"delivered_quantity": 1000, "cash_paid": 12518.0})
        record = self._act(record, "settle", "settlement_officer",
                           {"delivered_quantity": 500, "cash_paid": 12518.0})
        self.assertEqual(record["state"], "settled")

    def test_dividend_entitlement_lifecycle_and_reconciliation(self):
        record = self.service.create(Actor("creator", "trader"), "TRD-DIV-1", DIVIDEND_DATA)
        record = self._act(record, "apply_corporate", "corporate_actions")

        entitlements = self.service.list_entitlements(Actor("ops", "settlement_officer"), record_id=record["id"])
        self.assertEqual(len(entitlements), 1)
        entitlement = entitlements[0]
        self.assertEqual(entitlement["status"], "pending")
        self.assertEqual(entitlement["amount"], 500.0)
        self.assertFalse(entitlement["reconcilable"])

        # 入账前金额不可核对
        with self.assertRaises(Conflict):
            self.service.reconcile_entitlement(Actor("ops", "settlement_officer"), entitlement["id"], 500.0)

        record = self._act(record, "post_entitlement", "corporate_actions")
        self.assertEqual(record["state"], "adjusted")
        entitlement = self.service.get_entitlement(Actor("ops", "settlement_officer"), entitlement["id"])
        self.assertEqual(entitlement["status"], "booked")
        self.assertTrue(entitlement["reconcilable"])

        # 入账后金额才可核对
        result = self.service.reconcile_entitlement(Actor("ops", "settlement_officer"), entitlement["id"], 500.0)
        self.assertTrue(result["matched"])
        with self.assertRaises(Conflict):
            self.service.reconcile_entitlement(Actor("ops", "settlement_officer"), entitlement["id"], 501.0)

        # 重复入账被拒
        with self.assertRaises(ValidationError):
            self._act(record, "post_entitlement", "corporate_actions")

    def test_audit_timeline_records_quantity_price_amount_comparison(self):
        record = self.service.create(Actor("creator", "trader"), "TRD-DIV-2", DIVIDEND_DATA)
        record = self._act(record, "apply_corporate", "corporate_actions")
        timeline = self.service.timeline(Actor("creator", "trader"), record["id"])
        apply_event = next(event for event in timeline if event["action"] == "apply_corporate")
        comparison = apply_event["details"]["comparison"]
        self.assertEqual(comparison["corporate_action"], "dividend")
        self.assertEqual(comparison["before"]["quantity"], 1000)
        self.assertEqual(comparison["after"]["quantity"], 1000)
        self.assertEqual(comparison["before"]["amount"], 12500.0)
        self.assertEqual(comparison["after"]["price"], 12.5)
        self.assertEqual(comparison["cash_entitlement"]["amount"], 500.0)

        record = self._act(record, "post_entitlement", "corporate_actions")
        posted = self.service.repository.audit_timeline(record["id"])
        post_event = next(event for event in posted if event["action"] == "post_entitlement")
        self.assertEqual(post_event["details"]["cash_entitlement"]["status"], "booked")
        self.assertEqual(post_event["details"]["cash_entitlement"]["posted_amount"], 500.0)

    def test_no_corporate_action_cannot_be_applied(self):
        record = self.service.create(Actor("creator", "trader"), "TRD-NONE-1", base_data())
        with self.assertRaises(ValidationError):
            self._act(record, "apply_corporate", "corporate_actions")


if __name__ == "__main__":
    unittest.main()

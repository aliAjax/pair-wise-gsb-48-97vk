import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0, 'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'split', 'action_ratio': 2.0}
MERGER_DATA = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 10.0, 'fees': 10.0, 'currency': 'CNY', 'settlement_day': 3, 'corporate_action': 'merger', 'action_ratio': 0.5}
DIVIDEND_DATA = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0, 'currency': 'CNY', 'settlement_day': 4, 'corporate_action': 'dividend', 'action_ratio': 0.3}
FLOW = [('apply_corporate', 'corporate_actions', {}, 'adjusted'), ('approve', 'settlement_officer', {}, 'approved'), ('settle', 'settlement_officer', {'delivered_quantity': 2000, 'cash_paid': 12518.0}, 'settled')]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "trader"), "TRD-24001", CREATE_DATA)
        self.assertEqual(record["state"], "captured")
        for action, role, data, expected_state in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "trader"), record["id"])
        self.assertEqual(len(timeline), len(FLOW) + 1)
        self.assertEqual(timeline[-1]["action"], FLOW[-1][0])

    def test_merger_settles_on_effective_quantity(self):
        record = self.service.create(Actor("creator", "trader"), "TRD-25001", MERGER_DATA)
        record = self.service.act(Actor("ops", "corporate_actions"), record["id"], record["version"], "apply_corporate", {})
        self.assertEqual(record["payload"]["effective_quantity"], 500)
        record = self.service.act(Actor("ops", "settlement_officer"), record["id"], record["version"], "approve", {})
        # 交收按合并后的有效数量核对：仍按原数量交付会被拒绝
        with self.assertRaises(Exception):
            self.service.act(Actor("ops", "settlement_officer"), record["id"], record["version"], "settle", {'delivered_quantity': 1000, 'cash_paid': 10010.0})
        record = self.service.act(Actor("ops", "settlement_officer"), record["id"], record["version"], "settle", {'delivered_quantity': 500, 'cash_paid': 10010.0})
        self.assertEqual(record["state"], "settled")
        timeline = self.service.timeline(Actor("creator", "trader"), record["id"])
        applied = next(event for event in timeline if event["action"] == "apply_corporate")
        comparison = applied["details"]["comparison"]
        self.assertEqual(comparison["before"], {"quantity": 1000, "price": 10.0, "amount": 10000.0})
        self.assertEqual(comparison["after"], {"quantity": 500, "price": 20.0, "amount": 10000.0})

    def test_dividend_cash_entitlement_posting_and_reconciliation(self):
        record = self.service.create(Actor("creator", "trader"), "TRD-26001", DIVIDEND_DATA)
        with self.assertRaises(Exception):
            self.service.get_entitlement(Actor("creator", "trader"), record["id"])
        record = self.service.act(Actor("ops", "corporate_actions"), record["id"], record["version"], "apply_corporate", {})
        # 应用后生成一笔待入账现金权益：按股数1000×0.3=300，金额暂不可核对
        entitlement = self.service.get_entitlement(Actor("creator", "trader"), record["id"])
        self.assertEqual(entitlement["status"], "pending")
        self.assertEqual(entitlement["expected_amount"], 300.0)
        self.assertFalse(entitlement["reconcilable"])
        self.assertIsNone(entitlement["posted_amount"])
        with self.assertRaises(Conflict):
            self.service.reconcile_cash(Actor("creator", "trader"), record["id"])
        # 证券交收仍按企业行动后的有效数量（分红不改变股数）
        record = self.service.act(Actor("ops", "settlement_officer"), record["id"], record["version"], "approve", {})
        record = self.service.act(Actor("ops", "settlement_officer"), record["id"], record["version"], "settle", {'delivered_quantity': 1000, 'cash_paid': 12518.0})
        self.assertEqual(record["state"], "settled")
        # 现金入账后金额才可核对
        entitlement = self.service.post_cash(Actor("cashier", "settlement_officer"), record["id"], 1, {"posted_amount": 300.0})
        self.assertEqual(entitlement["status"], "posted")
        self.assertTrue(entitlement["reconcilable"])
        self.assertTrue(entitlement["matched"])
        self.assertEqual(entitlement["difference"], 0.0)
        reconciled = self.service.reconcile_cash(Actor("creator", "trader"), record["id"])
        self.assertTrue(reconciled["matched"])
        timeline = self.service.timeline(Actor("creator", "trader"), record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("post_cash", actions)
        posted_event = next(event for event in timeline if event["action"] == "post_cash")
        self.assertEqual(posted_event["details"]["expected_amount"], 300.0)
        self.assertEqual(posted_event["details"]["posted_amount"], 300.0)

    def test_dividend_amount_mismatch_flagged_on_reconciliation(self):
        record = self.service.create(Actor("creator", "trader"), "TRD-26002", DIVIDEND_DATA)
        record = self.service.act(Actor("ops", "corporate_actions"), record["id"], record["version"], "apply_corporate", {})
        entitlement = self.service.post_cash(Actor("cashier", "settlement_officer"), record["id"], 1, {"posted_amount": 280.0})
        self.assertTrue(entitlement["reconcilable"])
        self.assertFalse(entitlement["matched"])
        self.assertEqual(entitlement["difference"], -20.0)

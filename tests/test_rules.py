import unittest

from src.domain import Actor, ValidationError
from src.rules import DomainRules


CREATE_DATA = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0, 'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'split', 'action_ratio': 2.0}
MERGER_DATA = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 10.0, 'fees': 10.0, 'currency': 'CNY', 'settlement_day': 3, 'corporate_action': 'merger', 'action_ratio': 0.5}
DIVIDEND_DATA = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0, 'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'dividend', 'action_ratio': 0.3}
FLOW = [('apply_corporate', 'corporate_actions', {}, 'adjusted'), ('approve', 'settlement_officer', {}, 'approved'), ('settle', 'settlement_officer', {'delivered_quantity': 2000, 'cash_paid': 12518.0}, 'settled')]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        self.assertEqual(prepared["gross_amount"], 12500.0)
        self.assertEqual(prepared["net_amount"], 12518.0)
        self.assertEqual(prepared["adjusted_quantity"], 2000)

    def test_action_calculation(self):
        action, role, data, expected_state = FLOW[0]
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, summary, extras = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertTrue(payload["corporate_applied"])
        self.assertEqual(payload["effective_quantity"], 2000)
        comparison = extras["comparison"]
        self.assertEqual(comparison["before"]["quantity"], 1000)
        self.assertEqual(comparison["after"]["quantity"], 2000)
        self.assertAlmostEqual(comparison["before"]["price"], 12.5)
        self.assertAlmostEqual(comparison["after"]["price"], 6.25)
        self.assertEqual(comparison["before"]["amount"], comparison["after"]["amount"])

    def test_merger_ratio_adjusts_quantity_and_price(self):
        prepared = self.rules.prepare_create(MERGER_DATA)
        # 合并按换股比例缩减数量、上调均价，保持成本不变
        self.assertEqual(prepared["adjusted_quantity"], 500)
        self.assertAlmostEqual(prepared["adjusted_price"], 20.0, places=4)
        record = {"id": 2, "state": "captured", "payload": prepared}
        state, payload, summary, extras = self.rules.apply_action(record, "apply_corporate", {})
        self.assertEqual(state, "adjusted")
        self.assertEqual(payload["effective_quantity"], 500)
        self.assertAlmostEqual(payload["effective_price"], 20.0, places=4)
        comparison = extras["comparison"]
        self.assertEqual(comparison["corporate_action"], "merger")
        self.assertEqual(comparison["before"]["quantity"], 1000)
        self.assertEqual(comparison["after"]["quantity"], 500)
        self.assertEqual(comparison["before"]["amount"], comparison["after"]["amount"])
        self.assertNotIn("cash_entitlement", extras)

    def test_dividend_keeps_position_and_creates_pending_entitlement(self):
        prepared = self.rules.prepare_create(DIVIDEND_DATA)
        # 分红不改变持仓数量与均价，创建时不预生成权益
        self.assertEqual(prepared["adjusted_quantity"], 1000)
        self.assertAlmostEqual(prepared["adjusted_price"], 12.5, places=4)
        self.assertNotIn("cash_entitlement", prepared)
        record = {"id": 3, "state": "captured", "payload": prepared}
        state, payload, summary, extras = self.rules.apply_action(record, "apply_corporate", {})
        self.assertEqual(state, "adjusted")
        self.assertEqual(payload["effective_quantity"], 1000)
        self.assertAlmostEqual(payload["effective_price"], 12.5, places=4)
        entitlement = extras["cash_entitlement"]
        self.assertEqual(entitlement["status"], "pending")
        self.assertEqual(entitlement["quantity"], 1000)
        self.assertEqual(entitlement["expected_amount"], 300.0)
        comparison = extras["comparison"]
        self.assertEqual(comparison["before"]["cash_entitlement_amount"], 0.0)
        self.assertEqual(comparison["after"]["cash_entitlement_amount"], 300.0)
        self.assertEqual(comparison["before"]["quantity"], comparison["after"]["quantity"])
        self.assertEqual(comparison["before"]["amount"], comparison["after"]["amount"])

    def test_merger_ratio_too_small_rejected(self):
        invalid = dict(MERGER_DATA)
        invalid["action_ratio"] = 0.0004
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["side"] = 'hold'
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)

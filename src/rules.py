"""证券结算与企业行动处理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {'apply_corporate': {'corporate_actions'}, 'approve': {'settlement_officer'}, 'settle': {'settlement_officer'}, 'fail': {'settlement_officer'}, 'reverse': {'corporate_actions', 'settlement_officer'}, 'post_cash': {'settlement_officer'}}
TRANSITIONS = {'apply_corporate': {'captured': 'adjusted'}, 'approve': {'captured': 'approved', 'adjusted': 'approved'}, 'settle': {'approved': 'settled'}, 'fail': {'approved': 'failed'}, 'reverse': {'settled': 'reversed', 'failed': 'reversed'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "instrument")
        choice(p, "side", ["buy", "sell"])
        integer(p, "quantity", 1)
        number(p, "price", 0.01)
        number(p, "fees", 0)
        choice(p, "currency", ["CNY", "USD", "HKD"])
        integer(p, "settlement_day", 0)
        choice(p, "corporate_action", ["none", "split", "dividend", "merger"])
        number(p, "action_ratio", 0.01)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        gross = float(p["quantity"]) * float(p["price"])
        fee = float(p["fees"])
        p["gross_amount"] = round(gross, 2)
        p["net_amount"] = round(gross + fee if p["side"] == "buy" else gross - fee, 2)
        p["adjusted_quantity"] = p["quantity"]
        p["adjusted_price"] = p["price"]
        # 拆股与合并均按换股比例调整持仓数量与均价（数量×比例，均价÷比例，保持成本不变）
        if p["corporate_action"] in {"split", "merger"}:
            adjusted_quantity = int(round(float(p["quantity"]) * float(p["action_ratio"])))
            if adjusted_quantity <= 0:
                raise ValidationError("换股比例过小，调整后持仓数量不能为0")
            p["adjusted_quantity"] = adjusted_quantity
            p["adjusted_price"] = round(float(p["price"]) / float(p["action_ratio"]), 4)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"settled", "reversed"} and item["payload"].get("instrument") == payload.get("instrument") and item["payload"].get("settlement_day") == payload.get("settlement_day"):
                if item["payload"].get("side") == payload.get("side") and item["payload"].get("quantity") == payload.get("quantity") and item["payload"].get("price") == payload.get("price"):
                    raise Conflict("疑似重复结算指令")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, Dict[str, Any]]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        extras: Dict[str, Any] = {}
        if action == "apply_corporate":
            if p["corporate_action"] == "none":
                raise ValidationError("没有待处理的公司行动")
            corporate = p["corporate_action"]
            quantity = int(p["quantity"])
            price = float(p["price"])
            ratio = float(p["action_ratio"])
            if corporate in {"split", "merger"}:
                # 应用时按换股比例即时调整：数量×比例，均价÷比例（保持成本不变）
                effective_quantity = int(round(quantity * ratio))
                if effective_quantity <= 0:
                    raise ValidationError("换股比例过小，调整后持仓数量不能为0")
                effective_price = round(price / ratio, 4)
                changes["adjusted_quantity"] = effective_quantity
                changes["adjusted_price"] = effective_price
                changes["effective_quantity"] = effective_quantity
                changes["effective_price"] = effective_price
            else:
                # 现金分红不改变持仓数量与均价
                effective_quantity = quantity
                effective_price = price
                changes["effective_quantity"] = effective_quantity
                changes["effective_price"] = effective_price
            changes["corporate_applied"] = True
            # 应用前后数量、价格、金额的审计对照
            before_amount = round(quantity * price, 2)
            after_amount = round(effective_quantity * effective_price, 2)
            extras["comparison"] = {
                "corporate_action": corporate,
                "action_ratio": ratio,
                "before": {"quantity": quantity, "price": round(price, 4), "amount": before_amount},
                "after": {"quantity": effective_quantity, "price": round(effective_price, 4), "amount": after_amount},
            }
            if corporate == "dividend":
                # 按股数生成一笔待入账的现金权益，入账后金额才可核对
                entitlement_amount = round(quantity * ratio, 2)
                extras["cash_entitlement"] = {
                    "instrument": p["instrument"],
                    "currency": p["currency"],
                    "quantity": quantity,
                    "per_share_amount": round(ratio, 4),
                    "expected_amount": entitlement_amount,
                    "status": "pending",
                }
                extras["comparison"]["before"]["cash_entitlement_amount"] = 0.0
                extras["comparison"]["after"]["cash_entitlement_amount"] = entitlement_amount
            summary = {"split": "拆股已应用", "merger": "合并换股已应用", "dividend": "现金分红已应用"}.get(corporate, "公司行动已应用")
        elif action == "approve":
            changes["approved_amount"] = p["net_amount"]
            summary = "结算指令复核通过"
        elif action == "settle":
            delivered = integer(data, "delivered_quantity", 0)
            paid = number(data, "cash_paid", 0)
            required_quantity = int(p.get("effective_quantity", p["quantity"]))
            if delivered != required_quantity:
                raise ValidationError("交收证券数量不匹配")
            if paid < float(p["net_amount"]):
                raise ValidationError("交收资金不足")
            changes["delivered_quantity"] = delivered
            changes["cash_paid"] = paid
            summary = "交收完成"
        elif action == "fail":
            changes["fail_reason"] = text(data, "fail_reason")
            summary = "交收失败"
        elif action == "reverse":
            changes["reverse_reason"] = text(data, "reverse_reason")
            summary = "交收冲正"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action), extras

"""证券结算与企业行动处理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {'apply_corporate': {'corporate_actions'}, 'post_entitlement': {'corporate_actions', 'settlement_officer'}, 'approve': {'settlement_officer'}, 'settle': {'settlement_officer'}, 'fail': {'settlement_officer'}, 'reverse': {'corporate_actions', 'settlement_officer'}}
TRANSITIONS = {'apply_corporate': {'captured': 'adjusted'}, 'post_entitlement': {'adjusted': 'adjusted', 'approved': 'approved', 'settled': 'settled'}, 'approve': {'captured': 'approved', 'adjusted': 'approved'}, 'settle': {'approved': 'settled'}, 'fail': {'approved': 'failed'}, 'reverse': {'settled': 'reversed', 'failed': 'reversed'}}


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
        if p["corporate_action"] == "split":
            p["adjusted_quantity"] = int(float(p["quantity"]) * float(p["action_ratio"]))
            p["adjusted_price"] = round(float(p["price"]) / float(p["action_ratio"]), 4)
        elif p["corporate_action"] == "merger":
            # 换股合并：ratio份旧股换1股新股，数量与均价反向调整，保持持仓金额不变
            p["adjusted_quantity"] = int(float(p["quantity"]) / float(p["action_ratio"]))
            p["adjusted_price"] = round(float(p["price"]) * float(p["action_ratio"]), 4)
        return p

    @staticmethod
    def corporate_adjustment(payload: Dict[str, Any]) -> Dict[str, Any]:
        """返回企业行动应用后的有效数量、均价及现金权益（分红在应用时才生成）。"""
        quantity = int(payload["quantity"])
        price = float(payload["price"])
        ratio = float(payload["action_ratio"])
        action = payload["corporate_action"]
        if action == "split":
            adjusted_quantity = int(quantity * ratio)
            adjusted_price = round(price / ratio, 4)
            entitlement_amount = None
        elif action == "merger":
            adjusted_quantity = int(quantity / ratio)
            adjusted_price = round(price * ratio, 4)
            entitlement_amount = None
        elif action == "dividend":
            adjusted_quantity = quantity
            adjusted_price = price
            entitlement_amount = round(quantity * ratio, 2)
        else:
            adjusted_quantity = quantity
            adjusted_price = price
            entitlement_amount = None
        return {
            "adjusted_quantity": adjusted_quantity,
            "adjusted_price": adjusted_price,
            "effective_amount": round(adjusted_quantity * adjusted_price, 2),
            "cash_entitlement_amount": entitlement_amount,
        }

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

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "apply_corporate":
            if p.get("corporate_applied"):
                raise ValidationError("公司行动已应用")
            action_type = p["corporate_action"]
            if action_type == "none":
                raise ValidationError("没有待处理的公司行动")
            adjustment = self.corporate_adjustment(p)
            before = {
                "quantity": int(p["quantity"]),
                "price": round(float(p["price"]), 4),
                "amount": round(int(p["quantity"]) * float(p["price"]), 2),
            }
            changes["corporate_applied"] = True
            changes["adjusted_quantity"] = adjustment["adjusted_quantity"]
            changes["adjusted_price"] = adjustment["adjusted_price"]
            changes["effective_quantity"] = adjustment["adjusted_quantity"]
            changes["effective_price"] = adjustment["adjusted_price"]
            changes["effective_amount"] = adjustment["effective_amount"]
            changes["corporate_comparison"] = {
                "corporate_action": action_type,
                "action_ratio": float(p["action_ratio"]),
                "before": before,
                "after": {
                    "quantity": adjustment["adjusted_quantity"],
                    "price": adjustment["adjusted_price"],
                    "amount": adjustment["effective_amount"],
                },
            }
            if adjustment["cash_entitlement_amount"] is not None:
                changes["pending_cash_entitlement"] = {
                    "per_share": float(p["action_ratio"]),
                    "share_quantity": adjustment["adjusted_quantity"],
                    "amount": adjustment["cash_entitlement_amount"],
                    "status": "pending",
                }
                changes["corporate_comparison"]["cash_entitlement"] = {
                    "per_share": float(p["action_ratio"]),
                    "share_quantity": adjustment["adjusted_quantity"],
                    "amount": adjustment["cash_entitlement_amount"],
                }
            label = {"split": "拆股", "merger": "合并换股", "dividend": "现金分红"}[action_type]
            summary = "公司行动（%s）已应用" % label
        elif action == "post_entitlement":
            entitlement = p.get("pending_cash_entitlement")
            if not entitlement:
                raise ValidationError("没有待入账的现金权益")
            if entitlement.get("status") != "pending":
                raise ValidationError("现金权益已入账")
            amount = number(data, "posted_amount", 0) if "posted_amount" in data else float(entitlement["amount"])
            if amount != float(entitlement["amount"]):
                raise ValidationError("入账金额与现金权益金额不一致")
            changes["pending_cash_entitlement"] = dict(entitlement, status="booked", posted_amount=round(amount, 2))
            summary = "现金权益已入账"
        elif action == "approve":
            changes["approved_amount"] = p["net_amount"]
            summary = "结算指令复核通过"
        elif action == "settle":
            delivered = integer(data, "delivered_quantity", 0)
            paid = number(data, "cash_paid", 0)
            # 交收一律按企业行动应用后的有效数量核对证券
            required_quantity = int(p.get("effective_quantity", p["adjusted_quantity"]))
            if delivered != required_quantity:
                raise ValidationError("交收证券数量不匹配")
            if paid < float(p["net_amount"]):
                raise ValidationError("交收资金不足")
            changes["delivered_quantity"] = delivered
            changes["cash_paid"] = paid
            changes["settlement_check"] = {
                "effective_quantity": required_quantity,
                "effective_price": p.get("effective_price", p["adjusted_price"]),
                "delivered_quantity": delivered,
                "matched": delivered == required_quantity,
            }
            summary = "交收完成"
        elif action == "fail":
            changes["fail_reason"] = text(data, "fail_reason")
            summary = "交收失败"
        elif action == "reverse":
            changes["reverse_reason"] = text(data, "reverse_reason")
            summary = "交收冲正"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

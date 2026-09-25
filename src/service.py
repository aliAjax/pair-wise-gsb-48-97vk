"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        entitlement_op = None
        if action == "apply_corporate" and new_payload.get("corporate_action") == "dividend":
            pending = new_payload["pending_cash_entitlement"]
            entitlement_op = {
                "op": "create_pending",
                "entitlement": {
                    "instrument": new_payload["instrument"],
                    "currency": new_payload["currency"],
                    "per_share": pending["per_share"],
                    "share_quantity": pending["share_quantity"],
                    "amount": pending["amount"],
                },
            }
        elif action == "post_entitlement":
            entitlement_op = {
                "op": "post",
                "entitlement_id": int(new_payload["cash_entitlement_id"]),
                "posted_amount": new_payload["pending_cash_entitlement"]["posted_amount"],
            }
        comparison = new_payload.get("corporate_comparison") if action == "apply_corporate" else None
        if comparison is not None:
            details["comparison"] = comparison
        if action == "settle" and new_payload.get("settlement_check") is not None:
            details["settlement_check"] = new_payload["settlement_check"]
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
            entitlement_op=entitlement_op,
        )

    def list_entitlements(self, actor: Actor, record_id: int = None, status: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_entitlements(record_id=record_id, status=status, limit=limit)

    def get_entitlement(self, actor: Actor, entitlement_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_entitlement(entitlement_id)

    def reconcile_entitlement(self, actor: Actor, entitlement_id: int, actual_amount: float) -> Dict[str, Any]:
        """现金权益金额核对：仅入账(booked)后可核对。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        entitlement = self.repository.get_entitlement(entitlement_id)
        if entitlement["status"] != "booked":
            raise Conflict("现金权益尚未入账，金额不可核对")
        expected = round(float(entitlement["posted_amount"]), 2)
        actual = round(float(actual_amount), 2)
        if actual != expected:
            raise Conflict("现金权益金额核对不一致")
        return {"cash_entitlement_id": entitlement["id"], "expected_amount": expected, "actual_amount": actual, "matched": True}

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

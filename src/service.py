"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, number, text
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
        new_state, new_payload, summary, extras = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state, **extras},
            entitlement=extras.get("cash_entitlement"),
        )

    def get_entitlement(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        entitlement = self.repository.get_entitlement(record_id)
        if entitlement is None:
            raise NotFound("现金权益不存在")
        entitlement["reconcilable"] = entitlement["status"] == "posted"
        if entitlement["reconcilable"]:
            entitlement["matched"] = round(float(entitlement["posted_amount"]), 2) == round(float(entitlement["expected_amount"]), 2)
            entitlement["difference"] = round(float(entitlement["posted_amount"]) - float(entitlement["expected_amount"]), 2)
        return entitlement

    def post_cash(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "post_cash"):
            raise PermissionDenied("角色无权办理现金入账")
        record = self.repository.get(record_id)
        if not record["payload"].get("corporate_applied"):
            raise Conflict("企业行动尚未应用，现金权益不存在")
        posted_amount = number(data or {}, "posted_amount", 0.01)
        entitlement = self.repository.get_entitlement(record_id)
        if entitlement is None:
            raise NotFound("现金权益不存在")
        result = self.repository.post_entitlement(
            record_id=record_id,
            expected_version=int(expected_version),
            posted_amount=posted_amount,
            actor_id=actor.user_id,
            details={
                "summary": "现金分红入账",
                "from": "pending",
                "to": "posted",
                "expected_amount": entitlement["expected_amount"],
                "posted_amount": round(posted_amount, 2),
                "difference": round(posted_amount - float(entitlement["expected_amount"]), 2),
            },
        )
        result["reconcilable"] = True
        result["matched"] = round(float(result["posted_amount"]), 2) == round(float(result["expected_amount"]), 2)
        result["difference"] = round(float(result["posted_amount"]) - float(result["expected_amount"]), 2)
        return result

    def reconcile_cash(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        entitlement = self.get_entitlement(actor, record_id)
        if not entitlement["reconcilable"]:
            raise Conflict("现金权益尚未入账，金额不可核对")
        return entitlement

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

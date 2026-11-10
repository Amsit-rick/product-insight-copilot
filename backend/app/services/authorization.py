"""统一 RBAC + ABAC 授权决策入口。

迁移策略：保留现有 RBAC 和指标细粒度策略，把它们汇聚成同一种输入输出；
业务接口逐步接入，避免一次替换造成权限回归。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.auth.rbac import can_access
from app.models.identity import AccessPolicy
from app.services.metric_access import METRIC_RESOURCE_TYPES, VERDICT_ALLOWED, explain_access
from app.services.policy_conditions import evaluate_temporal_conditions

DecisionKind = Literal["allow", "deny", "require_approval"]


@dataclass(frozen=True)
class AuthorizationSubject:
    tenant_id: str
    user_id: str | None
    roles: list[str] = field(default_factory=list)
    department: str | None = None
    clearance: int = 0


@dataclass(frozen=True)
class AuthorizationResource:
    resource_type: str
    resource_id: str
    tenant_id: str | None = None
    department: str | None = None
    risk_tier: str | None = None
    amount: float | None = None
    classification: str | None = None
    tool_name: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthorizationRequest:
    subject: AuthorizationSubject
    action: str
    resource: AuthorizationResource
    context: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    decision: DecisionKind
    reason: str
    matched_policy_ids: list[str] = field(default_factory=list)
    effective_scope: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    @property
    def requires_approval(self) -> bool:
        return self.decision == "require_approval"

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "reason": self.reason,
            "matched_policy_ids": list(self.matched_policy_ids),
            "effective_scope": dict(self.effective_scope),
        }


def subject_from_principal(principal) -> AuthorizationSubject:  # noqa: ANN001
    return AuthorizationSubject(
        tenant_id=principal.tenant_id,
        user_id=principal.user.id,
        roles=list(principal.roles),
        department=principal.user.department,
        clearance=principal.user.security_level or 0,
    )


def _resource_matches(pattern: str, value: str) -> bool:
    pattern = (pattern or "").strip()
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return pattern == value


def _action_matches(actions: list[str] | str | None, action: str) -> bool:
    values = [actions] if isinstance(actions, str) else list(actions or [])
    normalized = {str(item).strip().lower() for item in values}
    action = action.strip().lower()
    return action in normalized or "*" in normalized or (
        "read" in normalized and action.endswith(":read")
    )


def _subject_matches(policy: AccessPolicy, subject: AuthorizationSubject) -> bool:
    identity = (policy.subject_id or "").strip()
    if identity in {"", "*"}:
        return True
    if (policy.subject_type or "role").lower() == "user":
        return identity == subject.user_id
    return identity in set(subject.roles)


def _as_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _condition_value(request: AuthorizationRequest, name: str) -> Any:
    if name in request.context:
        return request.context[name]
    if name == "department":
        return request.subject.department
    return getattr(request.resource, name, None)


def _conditions_match(
    policy: AccessPolicy, request: AuthorizationRequest
) -> tuple[bool, str]:
    conditions = policy.conditions or {}
    minimum = conditions.get("min_clearance")
    if minimum is not None:
        try:
            minimum_clearance = int(minimum)
        except (TypeError, ValueError):
            return False, "策略条件 min_clearance 非法"
        if request.subject.clearance < minimum_clearance:
            return False, "密级不足"

    for key, value_name in (
        ("departments", "department"),
        ("risk_tiers", "risk_tier"),
        ("classifications", "classification"),
        ("tool_names", "tool_name"),
    ):
        allowed = conditions.get(key)
        if allowed:
            allowed_values = {str(item) for item in (allowed if isinstance(allowed, list) else [allowed])}
            actual = _condition_value(request, value_name)
            if actual is None or str(actual) not in allowed_values:
                return False, f"条件 {key} 不匹配"

    amount = _as_decimal(_condition_value(request, "amount"))
    minimum_amount = _as_decimal(conditions.get("min_amount"))
    maximum_amount = _as_decimal(conditions.get("max_amount"))
    if minimum_amount is not None and (amount is None or amount < minimum_amount):
        return False, "金额低于策略适用范围"
    if maximum_amount is not None and (amount is None or amount > maximum_amount):
        return False, "金额超过策略适用范围"

    temporal_ok, temporal_reason = evaluate_temporal_conditions(
        conditions,
        occurred_at=request.occurred_at,
    )
    if not temporal_ok:
        return False, temporal_reason
    return True, ""


class AuthorizationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def decide(self, request: AuthorizationRequest) -> AuthorizationDecision:
        resource = request.resource
        subject = request.subject
        if resource.tenant_id and resource.tenant_id != subject.tenant_id:
            return AuthorizationDecision("deny", "资源不属于当前租户")

        if not can_access(subject.roles, request.action):
            return AuthorizationDecision(
                "deny", f"RBAC 未授予动作权限: {request.action}"
            )

        if resource.resource_type in METRIC_RESOURCE_TYPES and request.action in {
            "metric:read",
            "metric:read_all",
        }:
            explanation = explain_access(
                self.session,
                tenant_id=subject.tenant_id,
                roles=subject.roles,
                user_id=subject.user_id,
                clearance=subject.clearance,
                resource_name=resource.resource_id,
                resource_type=resource.resource_type,
            )
            if not explanation.decision.allowed:
                return AuthorizationDecision(
                    "deny",
                    explanation.decision.reason,
                    matched_policy_ids=[item.policy_id for item in explanation.verdicts],
                )
            approvals = [
                item
                for item in explanation.verdicts
                if item.code == VERDICT_ALLOWED and item.effect == "require_approval"
            ]
            if approvals:
                return AuthorizationDecision(
                    "require_approval",
                    "命中需要人工审批的指标访问策略",
                    matched_policy_ids=[item.policy_id for item in approvals],
                    effective_scope={"row_filters": explanation.effective_row_filters},
                )
            return AuthorizationDecision(
                "allow",
                "RBAC 与指标 ABAC 均允许",
                matched_policy_ids=explanation.allowed_policy_ids,
                effective_scope={"row_filters": explanation.effective_row_filters},
            )

        rows = (
            self.session.query(AccessPolicy)
            .filter(AccessPolicy.tenant_id.in_([subject.tenant_id, "*"]))
            .filter(AccessPolicy.resource_type == resource.resource_type)
            .all()
        )
        relevant = [
            row
            for row in rows
            if _resource_matches(row.resource_id, resource.resource_id)
            and _action_matches(row.actions, request.action)
        ]
        if not relevant:
            return AuthorizationDecision("allow", "RBAC 允许，未配置额外 ABAC 限制")

        applicable: list[AccessPolicy] = []
        condition_failures: list[str] = []
        for policy in relevant:
            if not _subject_matches(policy, subject):
                continue
            matched, reason = _conditions_match(policy, request)
            if matched:
                applicable.append(policy)
            elif reason:
                condition_failures.append(reason)

        denies = [row for row in applicable if (row.effect or "allow").lower() == "deny"]
        if denies:
            return AuthorizationDecision(
                "deny",
                "命中 ABAC 显式拒绝策略",
                matched_policy_ids=[row.id for row in denies],
            )
        approvals = [
            row
            for row in applicable
            if (row.effect or "allow").lower() == "require_approval"
        ]
        if approvals:
            return AuthorizationDecision(
                "require_approval",
                "命中需要人工审批的 ABAC 策略",
                matched_policy_ids=[row.id for row in approvals],
            )
        allows = [row for row in applicable if (row.effect or "allow").lower() == "allow"]
        if allows:
            return AuthorizationDecision(
                "allow",
                "RBAC 与 ABAC 均允许",
                matched_policy_ids=[row.id for row in allows],
                effective_scope={"conditions": [dict(row.conditions or {}) for row in allows]},
            )
        return AuthorizationDecision(
            "deny",
            "资源已配置 ABAC 白名单，但当前主体未匹配可用策略"
            + (f"（{'；'.join(sorted(set(condition_failures))) }）" if condition_failures else ""),
            matched_policy_ids=[row.id for row in relevant],
        )


__all__ = [
    "AuthorizationDecision",
    "AuthorizationRequest",
    "AuthorizationResource",
    "AuthorizationService",
    "AuthorizationSubject",
    "subject_from_principal",
]

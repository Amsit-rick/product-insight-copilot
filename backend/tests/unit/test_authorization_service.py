"""统一 RBAC + ABAC 授权决策入口。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.identity import AccessPolicy
from app.services.authorization import (
    AuthorizationRequest,
    AuthorizationResource,
    AuthorizationService,
    AuthorizationSubject,
)


def _subject(*, roles: list[str], tenant_id: str = "tenant-a") -> AuthorizationSubject:
    return AuthorizationSubject(
        tenant_id=tenant_id,
        user_id="user-1",
        roles=roles,
        department="after_sales",
        clearance=2,
    )


def _request(
    *,
    roles: list[str],
    action: str = "task:replay",
    resource_type: str = "task_replay",
    resource_id: str = "task-1",
    resource_tenant_id: str = "tenant-a",
    context: dict | None = None,
) -> AuthorizationRequest:
    return AuthorizationRequest(
        subject=_subject(roles=roles),
        action=action,
        resource=AuthorizationResource(
            resource_type=resource_type,
            resource_id=resource_id,
            tenant_id=resource_tenant_id,
        ),
        context=context or {},
    )


def test_rbac_基线先于_abac_执行(session):
    service = AuthorizationService(session)

    allowed = service.decide(_request(roles=["reviewer"]))
    denied = service.decide(_request(roles=["product_manager"]))

    assert allowed.decision == "allow"
    assert allowed.allowed is True
    assert allowed.matched_policy_ids == []
    assert denied.decision == "deny"
    assert denied.allowed is False
    assert "RBAC" in denied.reason


def test_跨租户资源在任何策略前直接拒绝(session):
    decision = AuthorizationService(session).decide(
        _request(roles=["admin"], resource_tenant_id="tenant-b")
    )

    assert decision.decision == "deny"
    assert decision.reason == "资源不属于当前租户"


def test_通用_abac_显式拒绝优先于_rbac_允许(session):
    policy = AccessPolicy(
        tenant_id="tenant-a",
        subject_type="role",
        subject_id="reviewer",
        resource_type="task_replay",
        resource_id="task-1",
        effect="deny",
        actions=["task:replay"],
        conditions={},
    )
    session.add(policy)
    session.commit()

    decision = AuthorizationService(session).decide(_request(roles=["reviewer"]))

    assert decision.decision == "deny"
    assert decision.matched_policy_ids == [policy.id]
    assert "显式拒绝" in decision.reason


def test_高风险资源可以要求人工审批(session):
    policy = AccessPolicy(
        tenant_id="tenant-a",
        subject_type="role",
        subject_id="reviewer",
        resource_type="task_replay",
        resource_id="task-1",
        effect="require_approval",
        actions=["task:replay"],
        conditions={"risk_tiers": ["high", "critical"]},
    )
    session.add(policy)
    session.commit()

    decision = AuthorizationService(session).decide(
        _request(roles=["reviewer"], context={"risk_tier": "high"})
    )

    assert decision.decision == "require_approval"
    assert decision.requires_approval is True
    assert decision.matched_policy_ids == [policy.id]


def test_指标读取复用现有策略并返回有效行级范围(session):
    policy = AccessPolicy(
        tenant_id="tenant-a",
        subject_type="role",
        subject_id="engineer",
        resource_type="metric",
        resource_id="dau",
        effect="allow",
        actions=["metric:read"],
        conditions={"row_filters": {"channel": ["channel_a"]}},
    )
    session.add(policy)
    session.commit()

    decision = AuthorizationService(session).decide(
        _request(
            roles=["engineer"],
            action="metric:read",
            resource_type="metric",
            resource_id="dau",
        )
    )

    assert decision.decision == "allow"
    assert decision.matched_policy_ids == [policy.id]
    assert decision.effective_scope == {"row_filters": {"channel": ["channel_a"]}}


def test_指标策略要求审批时不能被旧二态判定当作允许(session):
    policy = AccessPolicy(
        tenant_id="tenant-a",
        subject_type="role",
        subject_id="engineer",
        resource_type="metric",
        resource_id="finance_margin",
        effect="require_approval",
        actions=["metric:read"],
        conditions={},
    )
    session.add(policy)
    session.commit()

    decision = AuthorizationService(session).decide(
        _request(
            roles=["engineer"],
            action="metric:read",
            resource_type="metric",
            resource_id="finance_margin",
        )
    )

    assert decision.decision == "require_approval"
    assert decision.requires_approval is True
    assert decision.matched_policy_ids == [policy.id]


def test_非法策略条件失败关闭而不是抛异常(session):
    policy = AccessPolicy(
        tenant_id="tenant-a",
        subject_type="role",
        subject_id="reviewer",
        resource_type="task_replay",
        resource_id="task-1",
        effect="allow",
        actions=["task:replay"],
        conditions={"min_clearance": "not-a-number"},
    )
    session.add(policy)
    session.commit()

    decision = AuthorizationService(session).decide(_request(roles=["reviewer"]))

    assert decision.decision == "deny"
    assert "未匹配可用策略" in decision.reason


def test_通用资源时间窗按策略时区执行而不是静默忽略(session):
    policy = AccessPolicy(
        tenant_id="tenant-a",
        subject_type="role",
        subject_id="reviewer",
        resource_type="task_replay",
        resource_id="task-1",
        effect="allow",
        actions=["task:replay"],
        conditions={
            "time_window": {
                "start": "09:00",
                "end": "18:00",
                "days": [1, 2, 3, 4, 5],
                "timezone": "+08:00",
            }
        },
    )
    session.add(policy)
    session.commit()

    inside = AuthorizationService(session).decide(
        AuthorizationRequest(
            **{
                **_request(roles=["reviewer"]).__dict__,
                # 2026-11-05 是周四；02:00 UTC = 10:00 UTC+8。
                "occurred_at": datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc),
            }
        )
    )
    outside = AuthorizationService(session).decide(
        AuthorizationRequest(
            **{
                **_request(roles=["reviewer"]).__dict__,
                # 12:00 UTC = 20:00 UTC+8，已超出允许时间窗。
                "occurred_at": datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc),
            }
        )
    )

    assert inside.decision == "allow"
    assert outside.decision == "deny"
    assert "时间窗" in outside.reason


def test_departments_条件匹配访问者所属部门(session):
    policy = AccessPolicy(
        tenant_id="tenant-a",
        subject_type="role",
        subject_id="reviewer",
        resource_type="task_replay",
        resource_id="task-1",
        effect="allow",
        actions=["task:replay"],
        conditions={"departments": ["after_sales"]},
    )
    session.add(policy)
    session.commit()

    decision = AuthorizationService(session).decide(_request(roles=["reviewer"]))

    assert decision.decision == "allow"
    assert decision.matched_policy_ids == [policy.id]


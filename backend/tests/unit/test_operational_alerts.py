from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models.base import Base
from app.models.workflow import (
    AuditLog,
    OperationalAlert,
    ReflectionReleaseState,
)
from app.services.operational_alerts import (
    acknowledge_alert,
    raise_operational_alert,
    resolve_alert,
)
from app.services.reflection_release import (
    ReflectionReleaseDecision,
    ReflectionReleaseMetrics,
    release_state_for_rollout,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_same_fingerprint_is_deduplicated_and_increments_occurrence_count():
    session = _session()

    first = raise_operational_alert(
        session,
        tenant_id="tenant-a",
        kind="reflection_auto_rollback",
        severity="critical",
        title="反思灰度已自动回滚",
        message="安全门禁失败",
        fingerprint="reflection-auto-rollback",
    )
    second = raise_operational_alert(
        session,
        tenant_id="tenant-a",
        kind="reflection_auto_rollback",
        severity="critical",
        title="反思灰度已自动回滚",
        message="新的安全门禁失败原因",
        fingerprint="reflection-auto-rollback",
    )

    assert first.id == second.id
    assert second.occurrence_count == 2
    assert second.message == "新的安全门禁失败原因"
    assert session.query(OperationalAlert).count() == 1


def test_resolved_alert_is_reopened_when_same_problem_recurs():
    session = _session()
    alert = raise_operational_alert(
        session,
        tenant_id="tenant-a",
        kind="reflection_auto_rollback",
        severity="critical",
        title="反思灰度已自动回滚",
        message="安全门禁失败",
        fingerprint="reflection-auto-rollback",
    )
    resolve_alert(session, alert, actor_id="admin-1")

    reopened = raise_operational_alert(
        session,
        tenant_id="tenant-a",
        kind="reflection_auto_rollback",
        severity="critical",
        title="反思灰度已自动回滚",
        message="问题再次发生",
        fingerprint="reflection-auto-rollback",
    )

    assert reopened.status == "open"
    assert reopened.occurrence_count == 2
    assert reopened.resolved_at is None
    assert reopened.resolved_by is None


def test_same_fingerprint_is_isolated_by_tenant():
    session = _session()
    for tenant_id in ("tenant-a", "tenant-b"):
        raise_operational_alert(
            session,
            tenant_id=tenant_id,
            kind="reflection_auto_rollback",
            severity="critical",
            title="反思灰度已自动回滚",
            message="安全门禁失败",
            fingerprint="reflection-auto-rollback",
        )

    assert session.query(OperationalAlert).count() == 2


def test_acknowledge_records_actor_and_time_without_resolving():
    session = _session()
    alert = raise_operational_alert(
        session,
        tenant_id="tenant-a",
        kind="reflection_auto_rollback",
        severity="critical",
        title="反思灰度已自动回滚",
        message="安全门禁失败",
        fingerprint="reflection-auto-rollback",
    )

    acknowledge_alert(session, alert, actor_id="admin-1")

    assert alert.status == "acknowledged"
    assert alert.acknowledged_by == "admin-1"
    assert alert.acknowledged_at is not None
    assert alert.resolved_at is None


def test_automatic_reflection_rollback_creates_alert_and_separate_audit(monkeypatch):
    session = _session()
    state = ReflectionReleaseState(
        tenant_id="tenant-a",
        status="rolled_back",
        current_percent=0,
        recommended_percent=0,
        recommendation="rollback",
        reason="安全硬指标未达标",
        metrics={},
        version=2,
    )
    session.add(state)
    session.flush()
    decision = ReflectionReleaseDecision(
        recommendation="rollback",
        current_percent=25,
        recommended_percent=0,
        reason="安全硬指标未达标",
        requires_human_approval=False,
        auto_apply=True,
        metrics=ReflectionReleaseMetrics(treatment_sample=20, control_sample=20),
    )

    monkeypatch.setattr(
        "app.services.reflection_release.refresh_release_decision",
        lambda *_args, **_kwargs: (state, decision, True),
    )

    release_state_for_rollout(
        session,
        "tenant-a",
        Settings(_env_file=None, llm_provider="stub"),
    )

    alert = session.query(OperationalAlert).one()
    audit = session.query(AuditLog).one()
    assert alert.kind == "reflection_auto_rollback"
    assert alert.severity == "critical"
    assert alert.source_id == state.id
    assert audit.action == "reflection_release_changed"
    assert alert.id != audit.id

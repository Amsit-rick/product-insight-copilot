from __future__ import annotations

from datetime import timedelta

from app.config import Settings
from app.db.session import build_session_factory
from app.models.base import utcnow
from app.models.identity import Tenant
from app.models.scheduler import SchedulerLease
from app.models.workflow import AuditLog
from app import proactive_scheduler as scheduler_module
from app.proactive_scheduler import ProactiveAnomalyScheduler
from app.services.scheduler_lease import SqlSchedulerLease


def _settings(**overrides) -> Settings:
    values = {
        "llm_provider": "stub",
        "proactive_anomaly_enabled": True,
        "proactive_anomaly_metrics": ["next_day_retention"],
        "proactive_anomaly_interval_seconds": 300,
        "proactive_anomaly_lease_seconds": 900,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _lease(session) -> SqlSchedulerLease:
    return SqlSchedulerLease(build_session_factory(session.get_bind()))


def test_scheduler_lease_allows_only_one_owner(session):
    lease = _lease(session)

    assert lease.acquire("proactive-anomaly", "scheduler-a", lease_seconds=60) is True
    assert lease.acquire("proactive-anomaly", "scheduler-b", lease_seconds=60) is False
    assert lease.release("proactive-anomaly", "scheduler-a") is True
    assert lease.acquire("proactive-anomaly", "scheduler-b", lease_seconds=60) is True


def test_expired_scheduler_lease_can_be_taken_over(session):
    lease = _lease(session)
    assert lease.acquire("proactive-anomaly", "scheduler-a", lease_seconds=60) is True
    row = session.get(SchedulerLease, "proactive-anomaly")
    row.lease_expires_at = utcnow() - timedelta(seconds=1)
    session.commit()

    assert lease.acquire("proactive-anomaly", "scheduler-b", lease_seconds=60) is True
    session.expire_all()
    assert session.get(SchedulerLease, "proactive-anomaly").owner_id == "scheduler-b"


def test_only_current_owner_can_renew_scheduler_lease(session):
    lease = _lease(session)
    assert lease.acquire("proactive-anomaly", "scheduler-a", lease_seconds=60) is True

    assert lease.renew("proactive-anomaly", "scheduler-b", lease_seconds=60) is False
    assert lease.renew("proactive-anomaly", "scheduler-a", lease_seconds=60) is True


def test_disabled_scheduler_exits_without_scanning_or_creating_lease(session):
    scanned: list[str] = []
    scheduler = ProactiveAnomalyScheduler(
        settings=_settings(proactive_anomaly_enabled=False),
        session_factory=build_session_factory(session.get_bind()),
        lease=_lease(session),
        owner_id="scheduler-a",
        scanner=lambda _session, *, tenant_id, settings: scanned.append(tenant_id),
    )

    result = scheduler.run_once()

    assert result["status"] == "disabled"
    assert scanned == []
    assert session.query(SchedulerLease).count() == 0


def test_scheduler_scans_active_tenants_and_isolates_one_tenant_failure(session):
    active_a = Tenant(code="active-a", name="Active A", status="active")
    inactive = Tenant(code="inactive", name="Inactive", status="disabled")
    active_b = Tenant(code="active-b", name="Active B", status="active")
    session.add_all([active_a, inactive, active_b])
    session.commit()
    scanned: list[str] = []

    def scanner(_session, *, tenant_id, settings):
        scanned.append(tenant_id)
        if tenant_id == active_a.id:
            raise RuntimeError("tenant-a metric source unavailable")
        return {
            "anomalies_found": 2,
            "metrics_scanned": 1,
            "skipped": [{"metric_name": "crash_rate", "reason": "stale_metric_data"}],
        }

    scheduler = ProactiveAnomalyScheduler(
        settings=_settings(),
        session_factory=build_session_factory(session.get_bind()),
        lease=_lease(session),
        owner_id="scheduler-a",
        scanner=scanner,
    )

    result = scheduler.run_once()

    assert result["status"] == "partial"
    assert scanned == [active_a.id, active_b.id]
    assert result["tenants_total"] == 2
    assert result["tenants_succeeded"] == 1
    assert result["tenants_failed"] == 1
    assert result["anomalies_found"] == 2
    assert result["metrics_skipped"] == 1
    assert result["skipped_reasons"] == {"stale_metric_data": 1}
    session.expire_all()
    lease_row = session.get(SchedulerLease, "proactive-anomaly")
    assert lease_row.owner_id is None
    assert lease_row.last_status == "partial"
    assert lease_row.last_result["tenants_failed"] == 1
    audits = session.query(AuditLog).filter(
        AuditLog.action == "proactive_anomaly_scheduled_scan"
    ).all()
    assert len(audits) == 2
    assert {row.outcome for row in audits} == {"success", "failure"}


def test_scheduler_syncs_enabled_metric_source_before_scanning(session):
    tenant = Tenant(code="active-a", name="Active A", status="active")
    session.add(tenant)
    session.commit()
    events: list[str] = []

    def syncer(_session, *, tenant_id, settings):
        events.append(f"sync:{tenant_id}")
        return {"status": "synced", "rows_received": 2, "rows_written": 2}

    def scanner(_session, *, tenant_id, settings):
        events.append(f"scan:{tenant_id}")
        return {"anomalies_found": 0, "metrics_scanned": 1, "skipped": []}

    scheduler = ProactiveAnomalyScheduler(
        settings=_settings(metric_source_http_enabled=True),
        session_factory=build_session_factory(session.get_bind()),
        lease=_lease(session),
        owner_id="scheduler-a",
        metric_syncer=syncer,
        scanner=scanner,
    )

    result = scheduler.run_once()

    assert events == [f"sync:{tenant.id}", f"scan:{tenant.id}"]
    assert result["source_rows_received"] == 2
    assert result["source_rows_written"] == 2


def test_scheduler_fails_closed_when_metric_source_sync_fails(session):
    tenant = Tenant(code="active-a", name="Active A", status="active")
    session.add(tenant)
    session.commit()
    scanned: list[str] = []

    def syncer(_session, *, tenant_id, settings):
        raise RuntimeError("upstream unavailable")

    scheduler = ProactiveAnomalyScheduler(
        settings=_settings(metric_source_http_enabled=True),
        session_factory=build_session_factory(session.get_bind()),
        lease=_lease(session),
        owner_id="scheduler-a",
        metric_syncer=syncer,
        scanner=lambda _session, *, tenant_id, settings: scanned.append(tenant_id),
    )

    result = scheduler.run_once()

    assert result["status"] == "partial"
    assert result["tenants_failed"] == 1
    assert result["source_sync_failures"] == 1
    assert scanned == []


def test_scheduler_skips_when_another_instance_holds_lease(session):
    lease = _lease(session)
    assert lease.acquire("proactive-anomaly", "scheduler-a", lease_seconds=60) is True
    scanned: list[str] = []
    scheduler = ProactiveAnomalyScheduler(
        settings=_settings(),
        session_factory=build_session_factory(session.get_bind()),
        lease=lease,
        owner_id="scheduler-b",
        scanner=lambda _session, *, tenant_id, settings: scanned.append(tenant_id),
    )

    result = scheduler.run_once()

    assert result["status"] == "skipped_locked"
    assert scanned == []


def test_disabled_scheduler_main_exits_without_waiting_for_schema(monkeypatch):
    monkeypatch.setattr(
        scheduler_module,
        "get_settings",
        lambda: _settings(proactive_anomaly_enabled=False),
    )
    monkeypatch.setattr(
        scheduler_module,
        "wait_for_scheduler_schema",
        lambda: (_ for _ in ()).throw(AssertionError("disabled scheduler must not wait")),
    )

    assert scheduler_module.main(["--once"]) == 0


def test_wait_for_scheduler_schema_retries_until_migration_is_ready(monkeypatch):
    states = iter([False, False, True])
    sleeps: list[float] = []
    monkeypatch.setattr(scheduler_module, "scheduler_schema_ready", lambda: next(states))
    monkeypatch.setattr(scheduler_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    scheduler_module.wait_for_scheduler_schema(
        timeout_seconds=10,
        poll_interval_seconds=0.25,
    )

    assert sleeps == [0.25, 0.25]


def test_wait_for_scheduler_schema_times_out(monkeypatch):
    clock = iter([0.0, 0.1, 0.6])
    monkeypatch.setattr(scheduler_module, "scheduler_schema_ready", lambda: False)
    monkeypatch.setattr(scheduler_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(scheduler_module.time, "sleep", lambda _seconds: None)

    try:
        scheduler_module.wait_for_scheduler_schema(
            timeout_seconds=0.5,
            poll_interval_seconds=0.1,
        )
    except RuntimeError as exc:
        assert "scheduler_leases" in str(exc)
    else:
        raise AssertionError("schema 长期未就绪时必须超时失败")

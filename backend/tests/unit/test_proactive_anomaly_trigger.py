from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models.base import Base, utcnow
from app.models.identity import Tenant, User
from app.models.metrics import MetricDefinition, MetricSnapshot
from app.models.workflow import AnalysisTask, OperationalAlert, ToolCall
from app.services.proactive_anomaly import (
    ProactiveTriggerDisabled,
    create_diagnosis_for_alert,
    scan_tenant_anomalies,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _settings(**overrides) -> Settings:
    values = {
        "llm_provider": "stub",
        "proactive_anomaly_enabled": True,
        "proactive_anomaly_metrics": ["next_day_retention"],
        "proactive_anomaly_lookback_days": 14,
        "proactive_anomaly_method": "zscore",
        "proactive_anomaly_threshold": 1.5,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _seed_metric(session, *, latest_date: date | None = None):
    tenant = Tenant(code="tenant-a", name="Tenant A", status="active")
    session.add(tenant)
    session.flush()
    user = User(
        tenant_id=tenant.id,
        username="admin-a",
        full_name="Admin A",
        hashed_password="unused",
        security_level=4,
        is_active=True,
    )
    session.add(user)
    session.flush()
    session.add(
        MetricDefinition(
            tenant_id=tenant.id,
            name="next_day_retention",
            display_name="次日留存率",
            unit="percent",
            period="day",
            allowed_dimensions=[],
            higher_is_better=True,
        )
    )
    start = (latest_date or utcnow().date()) - timedelta(days=6)
    values = [0.41, 0.42, 0.40, 0.41, 0.42, 0.08, 0.41]
    for offset, value in enumerate(values):
        session.add(
            MetricSnapshot(
                tenant_id=tenant.id,
                metric_name="next_day_retention",
                stat_date=start + timedelta(days=offset),
                value=value,
                platform=None,
                app_version=None,
                channel=None,
                device=None,
                segment={},
                sample_size=1000,
                dataset_version="test-v1",
                source_version="test-source-v1",
                quality_flags=[],
            )
        )
    session.flush()
    return tenant, user


def test_disabled_trigger_fails_closed_without_reading_or_writing():
    session = _session()
    tenant, _user = _seed_metric(session)

    with pytest.raises(ProactiveTriggerDisabled):
        scan_tenant_anomalies(
            session,
            tenant_id=tenant.id,
            settings=_settings(proactive_anomaly_enabled=False),
        )

    assert session.query(OperationalAlert).count() == 0
    assert session.query(AnalysisTask).count() == 0
    assert session.query(ToolCall).count() == 0


def test_read_only_scan_creates_candidate_alert_but_not_diagnosis_task():
    session = _session()
    tenant, _user = _seed_metric(session)

    result = scan_tenant_anomalies(session, tenant_id=tenant.id, settings=_settings())

    assert result["read_only"] is True
    assert result["anomalies_found"] == 1
    alert = session.query(OperationalAlert).one()
    assert alert.kind == "proactive_metric_anomaly"
    assert alert.status == "open"
    assert alert.metadata_json["read_only_scan"] is True
    assert alert.metadata_json["metric_name"] == "next_day_retention"
    assert alert.metadata_json.get("diagnosis_task_id") is None
    assert session.query(AnalysisTask).count() == 0
    assert session.query(ToolCall).count() == 0


def test_stale_metric_data_is_skipped_without_creating_candidate():
    session = _session()
    tenant, _user = _seed_metric(
        session,
        latest_date=utcnow().date() - timedelta(days=10),
    )

    result = scan_tenant_anomalies(
        session,
        tenant_id=tenant.id,
        settings=_settings(proactive_anomaly_max_data_age_days=2),
    )

    assert result["metrics_scanned"] == 0
    assert result["anomalies_found"] == 0
    assert result["skipped"] == [
        {
            "metric_name": "next_day_retention",
            "reason": "stale_metric_data",
            "latest_stat_date": (utcnow().date() - timedelta(days=10)).isoformat(),
            "age_days": 10,
            "max_age_days": 2,
        }
    ]
    assert session.query(OperationalAlert).count() == 0


def test_metric_specific_freshness_threshold_overrides_global_default():
    session = _session()
    tenant, _user = _seed_metric(
        session,
        latest_date=utcnow().date() - timedelta(days=1),
    )

    result = scan_tenant_anomalies(
        session,
        tenant_id=tenant.id,
        settings=_settings(
            proactive_anomaly_max_data_age_days=2,
            proactive_anomaly_max_data_age_by_metric={"next_day_retention": 0},
        ),
    )

    assert result["metrics_scanned"] == 0
    assert result["skipped"][0]["reason"] == "stale_metric_data"
    assert result["skipped"][0]["max_age_days"] == 0
    assert session.query(OperationalAlert).count() == 0


def test_future_metric_data_is_skipped_without_creating_candidate():
    session = _session()
    tenant, _user = _seed_metric(
        session,
        latest_date=utcnow().date() + timedelta(days=1),
    )

    result = scan_tenant_anomalies(session, tenant_id=tenant.id, settings=_settings())

    assert result["metrics_scanned"] == 0
    assert result["anomalies_found"] == 0
    assert result["skipped"][0]["reason"] == "future_metric_data"
    assert result["skipped"][0]["age_days"] == -1
    assert session.query(OperationalAlert).count() == 0


def test_repeated_scan_deduplicates_same_candidate():
    session = _session()
    tenant, _user = _seed_metric(session)

    scan_tenant_anomalies(session, tenant_id=tenant.id, settings=_settings())
    scan_tenant_anomalies(session, tenant_id=tenant.id, settings=_settings())

    alert = session.query(OperationalAlert).one()
    assert alert.occurrence_count == 2


def test_human_confirmation_creates_one_idempotent_diagnosis_task():
    session = _session()
    tenant, user = _seed_metric(session)
    scan_tenant_anomalies(session, tenant_id=tenant.id, settings=_settings())
    alert = session.query(OperationalAlert).one()

    first, created = create_diagnosis_for_alert(session, alert=alert, user=user)
    second, created_again = create_diagnosis_for_alert(session, alert=alert, user=user)

    assert created is True
    assert created_again is False
    assert first.id == second.id
    assert first.filters["proactive_trigger"]["alert_id"] == alert.id
    assert first.filters["metrics"] == ["next_day_retention"]
    assert alert.status == "acknowledged"
    assert alert.metadata_json["diagnosis_task_id"] == first.id
    assert session.query(AnalysisTask).count() == 1


def test_non_proactive_alert_cannot_create_diagnosis_task():
    session = _session()
    tenant, user = _seed_metric(session)
    alert = OperationalAlert(
        tenant_id=tenant.id,
        kind="reflection_auto_rollback",
        severity="critical",
        title="其他告警",
        message="不是主动异常候选",
        status="open",
        fingerprint="other-alert",
        occurrence_count=1,
        first_occurred_at=utcnow(),
        last_occurred_at=utcnow(),
        metadata_json={},
    )
    session.add(alert)
    session.flush()

    with pytest.raises(ValueError, match="不是主动异常候选"):
        create_diagnosis_for_alert(session, alert=alert, user=user)

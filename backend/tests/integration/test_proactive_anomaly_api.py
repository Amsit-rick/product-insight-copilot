from __future__ import annotations

from app.services.scheduler_lease import SqlSchedulerLease


def test_proactive_trigger_is_disabled_by_default(client, auth):
    status = client.get("/api/v1/operational-alerts/proactive-trigger", headers=auth["admin"])

    assert status.status_code == 200
    assert status.json()["enabled"] is False
    assert status.json()["read_only_scan"] is True
    assert status.json()["max_data_age_days"] == 2
    assert status.json()["max_data_age_by_metric"] == {}
    assert status.json()["metric_source"] == {
        "enabled": False,
        "configured": False,
        "upstream_read_only": True,
    }

    scan = client.post(
        "/api/v1/operational-alerts/proactive-trigger/scan",
        headers=auth["admin"],
    )
    assert scan.status_code == 409
    assert scan.json()["detail"]["code"] == "proactive_trigger_disabled"


def test_proactive_trigger_status_exposes_last_scheduler_run(client, auth):
    lease = SqlSchedulerLease()
    assert lease.acquire("proactive-anomaly", "integration-scheduler", lease_seconds=60)
    assert lease.record_result(
        "proactive-anomaly",
        "integration-scheduler",
        status="partial",
        result={"tenants_total": 2, "tenants_failed": 1, "anomalies_found": 3},
        error="1 tenant scan(s) failed",
    )
    assert lease.release("proactive-anomaly", "integration-scheduler")

    response = client.get(
        "/api/v1/operational-alerts/proactive-trigger",
        headers=auth["admin"],
    )

    assert response.status_code == 200
    scheduler = response.json()["scheduler"]
    assert scheduler["running"] is False
    assert scheduler["last_status"] == "partial"
    assert scheduler["last_result"]["tenants_failed"] == 1
    assert "owner_id" not in scheduler


def test_proactive_scan_requires_admin(client, auth):
    response = client.post(
        "/api/v1/operational-alerts/proactive-trigger/scan",
        headers=auth["pm"],
    )
    assert response.status_code == 403


def test_enabled_scan_creates_candidate_then_human_creates_task(client, auth, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "proactive_anomaly_enabled", True)
    monkeypatch.setattr(settings, "proactive_anomaly_metrics", ["next_day_retention"])
    monkeypatch.setattr(settings, "proactive_anomaly_lookback_days", 40)
    monkeypatch.setattr(settings, "proactive_anomaly_max_data_age_days", 365)
    monkeypatch.setattr(settings, "proactive_anomaly_threshold", 1.0)
    monkeypatch.setattr("app.api.routes_operational_alerts.dispatch_task", lambda _task_id: None)

    scanned = client.post(
        "/api/v1/operational-alerts/proactive-trigger/scan",
        headers=auth["admin"],
    )
    assert scanned.status_code == 200, scanned.text
    payload = scanned.json()
    assert payload["read_only"] is True
    assert payload["anomalies_found"] >= 1

    listed = client.get("/api/v1/operational-alerts", headers=auth["admin"]).json()
    candidate = next(item for item in listed["items"] if item["kind"] == "proactive_metric_anomaly")
    assert candidate["metadata"]["diagnosis_task_id"] is None

    created = client.post(
        f"/api/v1/operational-alerts/{candidate['id']}/create-diagnosis",
        headers=auth["admin"],
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["task"]["id"]
    assert created.json()["created"] is True

    repeated = client.post(
        f"/api/v1/operational-alerts/{candidate['id']}/create-diagnosis",
        headers=auth["admin"],
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["created"] is False
    assert repeated.json()["task"]["id"] == task_id

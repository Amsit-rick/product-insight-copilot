from __future__ import annotations

import pytest


ENDPOINT = "/api/v1/operational-alerts"


@pytest.fixture(autouse=True)
def isolate_alerts(seeded_db):
    from app.db.session import SessionLocal
    from app.models.workflow import OperationalAlert, OperationalAlertDelivery

    session = SessionLocal()
    try:
        session.query(OperationalAlertDelivery).delete()
        session.query(OperationalAlert).delete()
        session.commit()
    finally:
        session.close()
    yield
    session = SessionLocal()
    try:
        session.query(OperationalAlertDelivery).delete()
        session.query(OperationalAlert).delete()
        session.commit()
    finally:
        session.close()


def _seed_alert(tenant_id: str | None = None) -> str:
    from app.db.session import SessionLocal
    from app.models.identity import User
    from app.services.operational_alerts import raise_operational_alert

    session = SessionLocal()
    try:
        if tenant_id is None:
            tenant_id = session.query(User).filter(User.username == "admin").one().tenant_id
        alert = raise_operational_alert(
            session,
            tenant_id=tenant_id,
            kind="reflection_auto_rollback",
            severity="critical",
            title="反思灰度已自动回滚",
            message="安全硬指标未达标，覆盖比例已回滚到 0%",
            fingerprint="reflection-auto-rollback",
            source_type="reflection_release",
            source_id="release-1",
        )
        session.commit()
        return alert.id
    finally:
        session.close()


def test_alert_api_requires_login(client):
    response = client.get(ENDPOINT)

    assert response.status_code == 401


def test_only_admin_can_read_and_change_operational_alerts(client, auth):
    _seed_alert()

    assert client.get(ENDPOINT, headers=auth["pm"]).status_code == 403
    assert client.get(ENDPOINT, headers=auth["admin"]).status_code == 200


def test_admin_can_list_acknowledge_and_resolve_alert(client, auth):
    alert_id = _seed_alert()

    listed = client.get(ENDPOINT, headers=auth["admin"])
    assert listed.status_code == 200, listed.text
    assert listed.json()["summary"] == {"open": 1, "acknowledged": 0, "critical": 1}
    assert listed.json()["items"][0]["id"] == alert_id

    acknowledged = client.post(f"{ENDPOINT}/{alert_id}/acknowledge", headers=auth["admin"])
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["status"] == "acknowledged"
    assert acknowledged.json()["acknowledged_by"]
    assert acknowledged.json()["acknowledged_at"]

    resolved = client.post(f"{ENDPOINT}/{alert_id}/resolve", headers=auth["admin"])
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_by"]
    assert resolved.json()["resolved_at"]


def test_alerts_are_scoped_to_current_tenant(client, auth):
    _seed_alert(tenant_id="other-tenant")

    listed = client.get(ENDPOINT, headers=auth["admin"])

    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert listed.json()["summary"] == {"open": 0, "acknowledged": 0, "critical": 0}


def test_admin_can_list_and_replay_dead_webhook_delivery(client, auth):
    from app.db.session import SessionLocal
    from app.models.workflow import OperationalAlert, OperationalAlertDelivery
    from app.services.operational_alerts import raise_operational_alert

    alert_id = _seed_alert()
    session = SessionLocal()
    try:
        alert = session.get(OperationalAlert, alert_id)
        delivery = OperationalAlertDelivery(
            alert_id=alert.id,
            tenant_id=alert.tenant_id,
            channel="webhook",
            occurrence_no=alert.occurrence_count,
            status="dead",
            payload={"event": "operational_alert"},
            attempts=3,
            max_attempts=3,
            last_error="HTTP 503",
        )
        session.add(delivery)
        other_alert = raise_operational_alert(
            session,
            tenant_id="other-tenant",
            kind="reflection_auto_rollback",
            severity="critical",
            title="其它租户告警",
            message="不应出现在当前租户汇总",
            fingerprint="reflection:auto-rollback",
        )
        session.add(
            OperationalAlertDelivery(
                alert_id=other_alert.id,
                tenant_id=other_alert.tenant_id,
                channel="webhook",
                occurrence_no=1,
                status="dead",
                payload={"event": "operational_alert"},
                attempts=3,
                max_attempts=3,
                last_error="HTTP 503",
            )
        )
        session.commit()
        delivery_id = delivery.id
    finally:
        session.close()

    denied = client.get(f"{ENDPOINT}/deliveries?status=dead", headers=auth["pm"])
    assert denied.status_code == 403

    listed = client.get(f"{ENDPOINT}/deliveries?status=dead", headers=auth["admin"])
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["id"] == delivery_id
    assert listed.json()["items"][0]["last_error"] == "HTTP 503"
    assert listed.json()["summary"]["dead"] == 1

    replayed = client.post(f"{ENDPOINT}/deliveries/{delivery_id}/replay", headers=auth["admin"])
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["status"] == "pending"
    assert replayed.json()["attempts"] == 0

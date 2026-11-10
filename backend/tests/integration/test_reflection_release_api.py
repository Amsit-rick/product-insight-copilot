from __future__ import annotations

import pytest


ENDPOINT = "/api/v1/experiments/reflection-release"


@pytest.fixture(autouse=True)
def isolate_release_state(seeded_db):
    from app.db.session import SessionLocal
    from app.models.workflow import AuditLog, ReflectionReleaseState

    session = SessionLocal()
    try:
        session.query(ReflectionReleaseState).delete()
        session.query(AuditLog).filter(
            AuditLog.action == "reflection_release_changed"
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()
    yield
    session = SessionLocal()
    try:
        session.query(ReflectionReleaseState).delete()
        session.query(AuditLog).filter(
            AuditLog.action == "reflection_release_changed"
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def test_reflection_release_report_requires_login(client):
    response = client.get(ENDPOINT)

    assert response.status_code == 401


def test_product_manager_can_read_but_cannot_change_release_state(client, auth):
    report = client.get(ENDPOINT, headers=auth["pm"])

    assert report.status_code == 200, report.text
    assert report.json()["decision"]["recommendation"] in {
        "hold",
        "promote",
        "pause",
        "rollback",
    }

    denied = client.post(
        f"{ENDPOINT}/decision",
        headers=auth["pm"],
        json={"action": "pause", "reason": "PM 不应能修改发布状态"},
    )
    assert denied.status_code == 403


def test_admin_can_pause_rollback_and_approve_a_staged_promotion(client, auth):
    paused = client.post(
        f"{ENDPOINT}/decision",
        headers=auth["admin"],
        json={"action": "pause", "reason": "延迟指标待核对"},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["state"]["status"] == "paused"

    rolled_back = client.post(
        f"{ENDPOINT}/decision",
        headers=auth["admin"],
        json={"action": "rollback", "reason": "安全演练"},
    )
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["state"]["status"] == "rolled_back"
    assert rolled_back.json()["state"]["current_percent"] == 0

    promoted = client.post(
        f"{ENDPOINT}/decision",
        headers=auth["admin"],
        json={"action": "approve_promotion", "percent": 10, "reason": "批准首批灰度"},
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["state"]["status"] == "active"
    assert promoted.json()["state"]["current_percent"] == 10
    assert promoted.json()["state"]["version"] >= 3


def test_reflection_release_changes_are_audited(client, auth):
    client.post(
        f"{ENDPOINT}/decision",
        headers=auth["admin"],
        json={"action": "rollback", "reason": "审计验证"},
    )

    audit = client.get(
        "/api/v1/audit-logs?action=reflection_release_changed",
        headers=auth["admin"],
    )

    assert audit.status_code == 200
    assert audit.json()["total"] >= 1
    latest = audit.json()["items"][0]
    assert latest["resource_type"] == "reflection_release"
    assert latest["detail"]["action"] == "rollback"


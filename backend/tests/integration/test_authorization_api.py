"""统一授权入口必须实际约束业务接口，而不是只提供预演。"""

from __future__ import annotations


def _create_task(client, auth, key: str) -> dict:
    response = client.post(
        "/api/v1/tasks",
        headers=auth["pm"],
        json={
            "query": "检查统一授权策略是否生效",
            "knowledge_bases": ["product_kb"],
            "idempotency_key": key,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def _create_policy(client, auth, *, task_id: str, effect: str) -> dict:
    response = client.post(
        "/api/v1/access-policies",
        headers=auth["admin"],
        json={
            "subject_type": "role",
            "subject_id": "reviewer",
            "resource_type": "task_replay",
            "resource_id": task_id,
            "effect": effect,
            "actions": ["task:replay"],
            "conditions": {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_显式拒绝策略实际阻断任务回放(client, auth):
    task = _create_task(client, auth, "authorization-deny-v1")
    policy = _create_policy(client, auth, task_id=task["id"], effect="deny")
    try:
        denied = client.post(
            f"/api/v1/tasks/{task['id']}/replay-snapshots",
            headers=auth["reviewer"],
        )
        assert denied.status_code == 403, denied.text
        detail = denied.json()["detail"]
        assert detail["code"] == "resource_forbidden"
        assert detail["decision"] == "deny"
        assert policy["id"] in detail["matched_policy_ids"]

        audit = client.get(
            "/api/v1/audit-logs?action=authorization_decision&limit=20",
            headers=auth["admin"],
        )
        assert audit.status_code == 200, audit.text
        rows = audit.json()["items"]
        assert any(
            row["resource_id"] == task["id"]
            and row["detail"].get("decision") == "deny"
            for row in rows
        )
    finally:
        client.delete(f"/api/v1/access-policies/{policy['id']}", headers=auth["admin"])


def test_需审批策略不能被当作普通允许(client, auth):
    task = _create_task(client, auth, "authorization-approval-v1")
    policy = _create_policy(client, auth, task_id=task["id"], effect="require_approval")
    try:
        response = client.post(
            f"/api/v1/tasks/{task['id']}/replay-snapshots",
            headers=auth["reviewer"],
        )
        assert response.status_code == 403, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "approval_required"
        assert detail["decision"] == "require_approval"
    finally:
        client.delete(f"/api/v1/access-policies/{policy['id']}", headers=auth["admin"])


def test_统一授权预演与真实业务使用同一决策(client, auth):
    task = _create_task(client, auth, "authorization-simulate-v1")

    baseline = client.post(
        "/api/v1/access-policies/authorize-simulate",
        headers=auth["admin"],
        json={
            "username": "reviewer",
            "action": "task:replay",
            "resource_type": "task_replay",
            "resource_id": task["id"],
            "context": {"risk_tier": task["risk_level"]},
        },
    )
    assert baseline.status_code == 200, baseline.text
    assert baseline.json()["decision"] == "allow"
    assert baseline.json()["subject"]["source"] == "account"

    policy = _create_policy(client, auth, task_id=task["id"], effect="require_approval")
    try:
        simulated = client.post(
            "/api/v1/access-policies/authorize-simulate",
            headers=auth["admin"],
            json={
                "username": "reviewer",
                "action": "task:replay",
                "resource_type": "task_replay",
                "resource_id": task["id"],
            },
        )
        assert simulated.status_code == 200, simulated.text
        assert simulated.json()["decision"] == "require_approval"
        assert policy["id"] in simulated.json()["matched_policy_ids"]

        actual = client.post(
            f"/api/v1/tasks/{task['id']}/replay-snapshots",
            headers=auth["reviewer"],
        )
        assert actual.status_code == 403
        assert actual.json()["detail"]["decision"] == simulated.json()["decision"]
    finally:
        client.delete(f"/api/v1/access-policies/{policy['id']}", headers=auth["admin"])


def test_指标需审批策略在真实查询中返回独立结论(client, auth):
    resource_id = "metric_requires_approval_test"
    created = client.post(
        "/api/v1/access-policies",
        headers=auth["admin"],
        json={
            "subject_type": "role",
            "subject_id": "engineer",
            "resource_type": "metric",
            "resource_id": resource_id,
            "effect": "require_approval",
            "actions": ["metric:read"],
            "conditions": {},
        },
    )
    assert created.status_code == 201, created.text
    try:
        response = client.get(
            "/api/v1/metrics/trend",
            params={
                "metric_name": resource_id,
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
            },
            headers=auth["eng"],
        )
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "approval_required"
        assert response.json()["detail"]["decision"] == "require_approval"
    finally:
        client.delete(
            f"/api/v1/access-policies/{created.json()['id']}",
            headers=auth["admin"],
        )


"""集成测试：指标密级策略的管理接口（界面"密级策略"页依赖它）。

三条底线：
1. **只有管理员能看策略清单**——"有哪些策略"本身就是信息，普通角色连列表都不该看到；
2. 写入必须过白名单校验（资源类型/效果/动作/条件键/行级维度），
   否则策略表会变成"注入列名"或"写入未知语义"的入口；
3. 增删都要有审计痕迹，且新增后**立即生效**（不需要重启）。
"""

from __future__ import annotations

import warnings

from app.db.session import SessionLocal
from app.models.identity import AccessPolicy


def _cleanup(resource_id: str) -> None:
    session = SessionLocal()
    try:
        session.query(AccessPolicy).filter(AccessPolicy.resource_id == resource_id).delete()
        session.commit()
    finally:
        session.close()


def test_仅管理员可查看策略清单(client, auth):
    assert client.get("/api/v1/access-policies", headers=auth["pm"]).status_code == 403
    assert client.get("/api/v1/access-policies", headers=auth["reviewer"]).status_code == 403
    resp = client.get("/api/v1/access-policies", headers=auth["admin"])
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert items, "演示租户应预置了密级策略"
    # 演示数据里有 finance 域与 order_conversion_rate 的授权
    resources = {(item["resource_type"], item["resource_id"]) for item in items}
    assert ("metric_domain", "finance") in resources
    assert ("metric", "order_conversion_rate") in resources


def test_新增策略后立即生效并可删除(client, auth):
    metric = "dau_test_scope"
    _cleanup(metric)
    try:
        created = client.post(
            "/api/v1/access-policies",
            headers=auth["admin"],
            json={
                "subject_type": "role",
                "subject_id": "product_manager",
                "resource_type": "metric",
                "resource_id": metric,
                "effect": "allow",
                "actions": ["metric:read"],
                "conditions": {"row_filters": {"channel": ["channel_a"]}},
            },
        )
        assert created.status_code == 201, created.text
        policy_id = created.json()["id"]
        assert created.json()["conditions"]["row_filters"]["channel"] == ["channel_a"]

        # 立即生效：pm 仍然能看到该指标（allow），但 eng 看不到（白名单语义）
        allowed = client.get(
            "/api/v1/metrics/trend",
            params={"metric_name": metric, "start_date": "2026-08-01", "end_date": "2026-08-31"},
            headers=auth["pm"],
        )
        denied = client.get(
            "/api/v1/metrics/trend",
            params={"metric_name": metric, "start_date": "2026-08-01", "end_date": "2026-08-31"},
            headers=auth["eng"],
        )
        assert allowed.status_code == 200, allowed.text
        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"]["code"] == "metric_forbidden"

        removed = client.delete(f"/api/v1/access-policies/{policy_id}", headers=auth["admin"])
        assert removed.status_code == 204
        # 删掉之后回到"无策略默认放行"
        assert (
            client.get(
                "/api/v1/metrics/trend",
                params={"metric_name": metric, "start_date": "2026-08-01", "end_date": "2026-08-31"},
                headers=auth["eng"],
            ).status_code
            == 200
        )
    finally:
        _cleanup(metric)

def test_重复策略被拒绝(client, auth):
    metric = "dau_test_dup"
    _cleanup(metric)
    payload = {
        "subject_type": "role",
        "subject_id": "product_manager",
        "resource_type": "metric",
        "resource_id": metric,
        "effect": "allow",
        "actions": ["metric:read"],
    }
    try:
        assert client.post("/api/v1/access-policies", headers=auth["admin"], json=payload).status_code == 201
        again = client.post("/api/v1/access-policies", headers=auth["admin"], json=payload)
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "duplicate_policy"
    finally:
        _cleanup(metric)


def test_非法字段被拒绝(client, auth):
    cases = [
        ({"resource_type": "database_table"}, "invalid_resource_type"),
        ({"effect": "maybe"}, "invalid_effect"),
        ({"subject_type": "group"}, "invalid_subject_type"),
        ({"actions": ["metric:write"]}, "invalid_actions"),
        ({"actions": []}, "invalid_actions"),
        ({"conditions": {"unknown_key": 1}}, "invalid_conditions"),
        ({"conditions": {"row_filters": {"DROP TABLE": ["x"]}}}, "invalid_row_filters"),
        (
            {"conditions": {"time_window": {"start": "99:00", "end": "18:00"}}},
            "invalid_conditions",
        ),
        ({"conditions": {"min_amount": 100, "max_amount": 10}}, "invalid_conditions"),
        ({"conditions": {"departments": []}}, "invalid_conditions"),
    ]
    base = {
        "subject_type": "role",
        "subject_id": "product_manager",
        "resource_type": "metric",
        "resource_id": "dau_test_invalid",
        "effect": "allow",
        "actions": ["metric:read"],
    }
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for override, expected_code in cases:
            resp = client.post(
                "/api/v1/access-policies", headers=auth["admin"], json={**base, **override}
            )
            assert resp.status_code == 422, f"{override} 应被拒绝，实际 {resp.status_code}"
            assert resp.json()["detail"]["code"] == expected_code, resp.text

    assert not [item for item in caught if "HTTP_422_UNPROCESSABLE_ENTITY" in str(item.message)]


def test_删除不存在的策略返回_404(client, auth):
    resp = client.delete("/api/v1/access-policies/does-not-exist", headers=auth["admin"])

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


def test_增删都写审计(client, auth):
    metric = "dau_test_audit"
    _cleanup(metric)
    try:
        created = client.post(
            "/api/v1/access-policies",
            headers=auth["admin"],
            json={
                "subject_type": "role",
                "subject_id": "reviewer",
                "resource_type": "metric",
                "resource_id": metric,
                "effect": "deny",
                "actions": ["metric:read"],
            },
        )
        assert created.status_code == 201
        client.delete(f"/api/v1/access-policies/{created.json()['id']}", headers=auth["admin"])
        logs = client.get("/api/v1/audit-logs?limit=50", headers=auth["admin"]).json()["items"]
        actions = {item["action"] for item in logs}
        assert "access_policy_created" in actions
        assert "access_policy_deleted" in actions
    finally:
        _cleanup(metric)

"""集成测试：指标数据范围（行级密级）在接口层的行为。

三类断言，缺一不可：
1. 范围内请求正常返回数据；
2. **越界维度值返回 403**（而不是空结果——那会被误读成"这个渠道没数据"）；
3. 越权/越界都不产出数值，也不泄露"还有哪些值存在"。
"""

from __future__ import annotations

import pytest

from app.db.session import SessionLocal
from app.models.identity import AccessPolicy, Tenant

METRIC = "dau"
QUERY = {
    "metric_name": METRIC,
    "start_date": "2026-08-01",
    "end_date": "2026-08-31",
}


@pytest.fixture()
def scoped_policy():
    """给 pm 角色加一条"只能看 channel_a"的策略（dau 这个指标本身没有其它策略）。"""
    session = SessionLocal()
    try:
        tenant = session.query(Tenant).filter(Tenant.code == "demo").first()
        row = AccessPolicy(
            tenant_id=tenant.id,
            subject_type="role",
            subject_id="product_manager",
            resource_type="metric",
            resource_id=METRIC,
            effect="allow",
            actions=["metric:read"],
            conditions={"row_filters": {"channel": ["channel_a"]}},
        )
        session.add(row)
        session.commit()
        yield row.id
    finally:
        session.query(AccessPolicy).filter(AccessPolicy.resource_id == METRIC).delete()
        session.commit()
        session.close()


def test_trend_within_scope_returns_data(client, auth, scoped_policy):
    resp = client.get("/api/v1/metrics/trend", params=QUERY, headers=auth["pm"])

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["rows"], "范围内应该能看到自己的那份数据"
    assert any("数据范围" in warning for warning in payload["warnings"])


def test_requesting_out_of_scope_channel_is_forbidden(client, auth, scoped_policy):
    """显式请求越界渠道：必须 403 说清原因，不能返回空数据。"""
    resp = client.get(
        "/api/v1/metrics/trend",
        params={**QUERY, "channel": "channel_b"},
        headers=auth["pm"],
    )

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "metric_out_of_scope"
    assert "channel_b" in detail["message"]
    assert "数据范围" in detail["message"]


def test_breakdown_only_contains_allowed_values(client, auth, scoped_policy):
    resp = client.get(
        "/api/v1/metrics/breakdown",
        params={**QUERY, "dimension": "channel"},
        headers=auth["pm"],
    )

    assert resp.status_code == 200, resp.text
    keys = {row["key"] for row in resp.json()["rows"]}
    assert keys <= {"channel_a"}, f"分群泄露了越界渠道: {keys}"


def test_coverage_respects_data_range(client, auth, scoped_policy):
    resp = client.get("/api/v1/metrics/coverage", params={"metric_name": METRIC}, headers=auth["pm"])

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["rows"] > 0
    assert "已按数据范围策略过滤" in payload.get("data_range_note", "")


def test_admin_without_scope_specific_policy_still_limited_by_whitelist(client, auth, scoped_policy):
    """白名单语义：admin 没有被这条策略允许，因此同样查不到（这是有意的收紧行为）。"""
    resp = client.get("/api/v1/metrics/trend", params=QUERY, headers=auth["admin"])

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "metric_forbidden"


def test_other_metrics_unaffected(client, auth, scoped_policy):
    """范围策略只作用于被授权的那个指标，不该连坐其它指标。"""
    resp = client.get(
        "/api/v1/metrics/trend",
        params={**QUERY, "metric_name": "crash_rate"},
        headers=auth["pm"],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"]

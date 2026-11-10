"""集成测试：权限预演接口（界面「密级策略」页的"为什么看不到"）。

这一层要钉住四件事：

1. **只有管理员能用**——预演会暴露"谁能看什么"，本身就是敏感信息；
2. 预演结论与**该账号真实请求的结果一致**（最关键的一条：预演说看不到，真实查询就必须被拒）；
3. 逐条策略结论要能解释白名单拒绝、显式拒绝、密级不足这些分支；
4. 行级范围预演要能直接回答"我请求 channel_b 会不会越界"。
"""

from __future__ import annotations

from app.db.session import SessionLocal
from app.models.identity import AccessPolicy

SIMULATE = "/api/v1/access-policies/simulate"


def _cleanup(resource_id: str) -> None:
    session = SessionLocal()
    try:
        session.query(AccessPolicy).filter(AccessPolicy.resource_id == resource_id).delete()
        session.commit()
    finally:
        session.close()


def test_仅管理员可预演(client, auth):
    payload = {"username": "eng", "metric": "order_conversion_rate"}
    for account in ("pm", "eng", "ops", "reviewer"):
        resp = client.post(SIMULATE, headers=auth[account], json=payload)
        assert resp.status_code == 403, f"{account} 不应能调用预演：{resp.status_code}"


def test_演示数据_受限指标对研发不可见(client, auth):
    """eng 不在 order_conversion_rate 的授权角色里 → 拒绝，且逐条结论能说明原因。"""
    resp = client.post(SIMULATE, headers=auth["admin"], json={"username": "eng", "metric": "order_conversion_rate"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["allowed"] is False
    assert body["has_policies"] is True
    assert body["subject"]["username"] == "eng"
    assert body["subject"]["source"] == "account"
    assert body["subject"]["roles"] == ["engineer"]
    assert body["subject"]["clearance"] == 2
    assert body["matched_policy_id"] is None
    assert "未对当前角色开放" in body["reason"]
    # 三条给 admin/pm/reviewer 的 allow 都存在，对 eng 而言全是"主体不匹配"
    assert {item["code"] for item in body["verdicts"]} == {"subject_mismatch"}
    assert all(item["resource_id"] == "order_conversion_rate" for item in body["verdicts"])


def test_演示数据_受限指标对产品经理可见(client, auth):
    resp = client.post(SIMULATE, headers=auth["admin"], json={"username": "pm", "metric": "order_conversion_rate"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["allowed"] is True
    assert body["matched_policy_id"]
    # 主体的展示形式是 "role:product_manager"（subject_type:subject_id），便于直接看出策略给的是谁
    assert "role:product_manager" in [
        item["subject"] for item in body["verdicts"] if item["code"] == "allowed"
    ]
    assert body["effective_row_filters"] == {}


def test_演示数据_显式拒绝优先于允许(client, auth):
    """finance_raw_ledger 有一条 role:* 的 deny：连管理员也应被拒。"""
    resp = client.post(SIMULATE, headers=auth["admin"], json={"username": "admin", "metric": "finance_raw_ledger"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["allowed"] is False
    assert "命中显式拒绝策略" in body["reason"]
    assert {item["code"] for item in body["verdicts"]} == {"explicit_deny"}
    # role:* 表示"所有角色"，展示成 role:* 比裸 * 更能说明这条策略的作用域
    assert body["verdicts"][0]["subject"] == "role:*"


def test_指标域预演(client, auth):
    """resource_type=metric_domain 也走同一套判定。"""
    allowed = client.post(
        SIMULATE,
        headers=auth["admin"],
        json={"username": "pm", "metric": "finance", "resource_type": "metric_domain"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["allowed"] is True
    assert allowed.json()["resource"]["type"] == "metric_domain"

    denied = client.post(
        SIMULATE,
        headers=auth["admin"],
        json={"username": "ops", "metric": "finance", "resource_type": "metric_domain"},
    )
    assert denied.status_code == 200, denied.text
    assert denied.json()["allowed"] is False


def test_预演结论与真实查询一致(client, auth):
    """最重要的断言：预演说"看不到"，那么该账号真实查询就**必须**被拒绝。

    预演如果和真实判定分叉，管理员看到的解释就是错的 —— 那比没有预演更危险。
    """
    for username, expect_allowed in (("pm", True), ("eng", False)):
        simulated = client.post(
            SIMULATE, headers=auth["admin"], json={"username": username, "metric": "order_conversion_rate"}
        ).json()
        real = client.get(
            "/api/v1/metrics/trend",
            headers=auth[username],
            params={
                "metric_name": "order_conversion_rate",
                # 趋势接口的入参是 start_date / end_date（不是 date_from/date_to）
                "start_date": "2026-08-10",
                "end_date": "2026-08-20",
            },
        )
        if expect_allowed:
            assert simulated["allowed"] is True
            assert real.status_code == 200, f"{username} 预演放行但真实查询 {real.status_code}"
        else:
            assert simulated["allowed"] is False
            assert real.status_code == 403, f"{username} 预演拒绝但真实查询 {real.status_code}"
            # 拒绝原因也要一致（预演给的原因直接来自判定层，不是另写的文案）
            detail = real.json()["detail"]
            assert simulated["reason"] == (detail["message"] if isinstance(detail, dict) else detail)


def test_未知账号返回_404(client, auth):
    resp = client.post(SIMULATE, headers=auth["admin"], json={"username": "no_such_user", "metric": "dau"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "user_not_found"


def test_未提供主体返回_422(client, auth):
    assert client.post(SIMULATE, headers=auth["admin"], json={"metric": "dau"}).status_code == 422
    assert client.post(SIMULATE, headers=auth["admin"], json={"metric": "dau", "roles": []}).status_code == 422


def test_可直接按角色与密级预演(client, auth):
    """不依赖账号：验证"换成这个角色/密级之后会怎样"。"""
    body = client.post(
        SIMULATE,
        headers=auth["admin"],
        json={"roles": ["engineer"], "clearance": 2, "metric": "order_conversion_rate"},
    ).json()
    assert body["subject"]["source"] == "explicit"
    assert body["subject"]["username"] is None
    assert body["allowed"] is False

    # 同一指标换成 product_manager 就应放行（对照组）
    allowed = client.post(
        SIMULATE,
        headers=auth["admin"],
        json={"roles": ["product_manager"], "clearance": 2, "metric": "order_conversion_rate"},
    ).json()
    assert allowed["allowed"] is True


def test_行级范围预演直接回答越界与否(client, auth):
    """加一条"只能看 channel_a"的策略，预演应能回答 channel_b 会不会越界。"""
    metric = "dau_scope_sim"
    _cleanup(metric)
    try:
        created = client.post(
            "/api/v1/access-policies",
            headers=auth["admin"],
            json={
                "subject_type": "role",
                "subject_id": "engineer",
                "resource_type": "metric",
                "resource_id": metric,
                "effect": "allow",
                "actions": ["metric:read"],
                "conditions": {"row_filters": {"channel": ["channel_a"]}},
            },
        )
        assert created.status_code == 201, created.text

        inside = client.post(
            SIMULATE,
            headers=auth["admin"],
            json={"username": "eng", "metric": metric, "requested": {"channel": "channel_a"}},
        ).json()
        assert inside["allowed"] is True
        assert inside["effective_row_filters"] == {"channel": ["channel_a"]}
        assert inside["request_violation"] is None

        outside = client.post(
            SIMULATE,
            headers=auth["admin"],
            json={"username": "eng", "metric": metric, "requested": {"channel": "channel_b"}},
        ).json()
        assert outside["allowed"] is True  # 指标本身能读
        assert outside["request_violation"], "请求 channel_b 应被判为越界"
        assert "channel_b" in outside["request_violation"]
    finally:
        _cleanup(metric)


def test_待验证维度必须走白名单(client, auth):
    resp = client.post(
        SIMULATE,
        headers=auth["admin"],
        json={"username": "pm", "metric": "dau", "requested": {"evil_column": "x"}},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_dimension"


def test_预演写审计(client, auth):
    """预演是只读的，但"谁查过谁的权限"本身要留痕。"""
    # 注意：审计接口的参数是 limit，返回 {items: [...]}（没有分页 meta）
    before = client.get("/api/v1/audit-logs?action=policy_simulated&limit=100", headers=auth["admin"]).json()
    client.post(SIMULATE, headers=auth["admin"], json={"username": "ops", "metric": "order_conversion_rate"})
    after = client.get("/api/v1/audit-logs?action=policy_simulated&limit=100", headers=auth["admin"]).json()
    assert len(after["items"]) == len(before["items"]) + 1
    latest = after["items"][0]
    assert latest["action"] == "policy_simulated"
    assert latest["resource_id"] == "order_conversion_rate"

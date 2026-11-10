"""政策版本化与生效期过滤的集成测试（需求书 §4.3）。

验收要点（每条都是"错了会出合规事故"的）：

1. 清单里三个版本各自的状态：现行 / 已失效 / 未生效；
2. **检索层真的排除了失效与未生效版本**（不是只在界面上标注）；
3. 检索测试接口能说明"为什么某版本没被用上"；
4. 适用范围限定了店铺而工单没有店铺信息时，判为不适用（保守）；
5. 只有 `policy:write` 才能新建政策（客服不能改政策）。
"""

from __future__ import annotations

import pytest


def _policies(client, auth, **params) -> list[dict]:
    resp = client.get("/api/v1/policies", params=params, headers=auth["ops"])
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def test_seeded_policy_versions_have_expected_states(client, auth):
    items = _policies(client, auth)
    by_version = {item["version"]: item for item in items}
    assert {"v1.0", "v3.0", "v4.0"} <= set(by_version), by_version.keys()

    assert by_version["v1.0"]["state"] == "expired"
    assert by_version["v1.0"]["usable"] is False
    assert "已失效" in by_version["v1.0"]["reason"]

    assert by_version["v3.0"]["state"] == "active"
    assert by_version["v3.0"]["usable"] is True

    assert by_version["v4.0"]["state"] == "future"
    assert by_version["v4.0"]["usable"] is False
    assert "未生效" in by_version["v4.0"]["reason"]


def test_expired_and_future_policies_are_excluded_from_search(client, auth):
    """核心断言：检索结果里**不能出现**失效与未生效版本的政策切片。"""
    items = _policies(client, auth)
    by_version = {item["version"]: item for item in items}
    expired_doc = by_version["v1.0"]["document_id"]
    future_doc = by_version["v4.0"]["document_id"]
    current_doc = by_version["v3.0"]["document_id"]

    resp = client.post(
        "/api/v1/policies/search-test",
        json={"query": "物流停滞补偿标准 停滞多久可以补偿多少钱", "top_k": 8},
        headers=auth["ops"],
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    hits = payload["hits"]
    assert hits, "现行政策应当能被检索到"
    doc_ids = {hit["document_id"] for hit in hits}
    assert expired_doc not in doc_ids, "已失效政策不得出现在检索结果里"
    assert future_doc not in doc_ids, "未生效政策不得出现在检索结果里"
    assert current_doc in doc_ids, "现行政策必须能被检索到"
    assert all(hit["usable"] is True for hit in hits)
    assert set(payload["excluded_document_ids"]) >= {expired_doc, future_doc}


def test_search_test_explains_why_versions_are_unusable(client, auth):
    resp = client.post(
        "/api/v1/policies/search-test",
        json={"query": "停滞补偿", "top_k": 5},
        headers=auth["ops"],
    )
    payload = resp.json()
    excluded = {item["version"]: item for item in payload["excluded_policies"]}
    assert "v1.0" in excluded and "v4.0" in excluded
    assert excluded["v1.0"]["state"] == "expired"
    assert "已失效" in excluded["v1.0"]["reason"]
    assert "未生效" in excluded["v4.0"]["reason"]


def test_shop_scoped_policy_needs_matching_shop(client, auth):
    """v4.0 限定自营店铺：即使把时间调到它生效之后，拿着别的店铺也不能用。"""
    future_ok = _policies(client, auth, at="2027-06-01", platform="Android", shop_id="self_operated")
    by_version = {item["version"]: item for item in future_ok}
    assert by_version["v4.0"]["usable"] is True

    other_shop = _policies(client, auth, at="2027-06-01", platform="Android", shop_id="pop_shop_1")
    by_version = {item["version"]: item for item in other_shop}
    assert by_version["v4.0"]["usable"] is False
    assert "仅适用店铺" in by_version["v4.0"]["reason"]

    # 工单没有店铺信息时同样保守判为不适用
    no_shop = _policies(client, auth, at="2027-06-01", platform="Android")
    by_version = {item["version"]: item for item in no_shop}
    assert by_version["v4.0"]["usable"] is False
    assert "未知" in by_version["v4.0"]["reason"]


def test_policy_write_requires_permission(client, auth):
    payload = {
        "title": "测试政策",
        "version": "v9.9",
        "content": "这是一条用于测试权限与生效期的政策正文，内容长度需要超过十个字符。",
    }
    denied = client.post("/api/v1/policies/documents", json=payload, headers=auth["ops"])
    assert denied.status_code == 403

    allowed = client.post("/api/v1/policies/documents", json=payload, headers=auth["reviewer"])
    assert allowed.status_code == 201, allowed.text
    created = allowed.json()
    assert created["version"] == "v9.9"

    # 新建的政策应立即出现在清单里并可用
    items = _policies(client, auth)
    assert any(item["id"] == created["id"] and item["usable"] for item in items)


def test_invalid_effective_window_is_rejected(client, auth):
    resp = client.post(
        "/api/v1/policies/documents",
        json={
            "title": "时间窗写反的政策",
            "version": "v0.1",
            "content": "生效日期晚于失效日期，应当被接口直接拒绝掉，不允许入库。",
            "effective_from": "2026-10-01",
            "effective_to": "2026-09-01",
        },
        headers=auth["reviewer"],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_effective_window"


def test_archive_makes_policy_unusable(client, auth):
    created = client.post(
        "/api/v1/policies/documents",
        json={
            "title": "将被归档的政策",
            "version": "v8.8",
            "content": "该政策用于验证归档后不可引用，正文需要足够长以满足校验要求。",
        },
        headers=auth["reviewer"],
    ).json()
    resp = client.post(
        f"/api/v1/policies/documents/{created['id']}/status",
        json={"status": "archived", "reason": "被 v9.0 取代"},
        headers=auth["reviewer"],
    )
    assert resp.status_code == 200
    items = _policies(client, auth)
    row = next(item for item in items if item["id"] == created["id"])
    assert row["usable"] is False
    assert "归档" in row["reason"]

    # 变更理由必须落审计：政策下线是合规动作，事后要能回答"谁因为什么把它停了"
    # （此前接口只收 status，界面上的"理由必填"其实存不下来）
    from app.db.session import SessionLocal
    from app.models.workflow import AuditLog

    session = SessionLocal()
    try:
        rows = (
            session.query(AuditLog)
            .filter(AuditLog.resource_id == created["id"])
            .all()
        )
        audit = next(item for item in rows if item.action == "policy_status_changed")
        assert audit.detail.get("reason") == "被 v9.0 取代", audit.detail
        assert audit.detail.get("status") == "archived"
    finally:
        session.close()


def test_member_level_scope_is_usable_from_list_and_search(client, auth):
    """会员等级也是适用范围维度：接口必须能传，否则"仅 gold 会员"的政策永远判不适用。

    回归背景（界面同学发现）：`applicability()` 一直支持 `member_level`，但 `GET /policies`
    与 `POST /policies/search-test` 都没有这个参数 —— 于是界面上无论怎么点，
    这类政策都显示"不适用（当前工单为 未知）"，等于这个能力被接口卡死。
    """
    created = client.post(
        "/api/v1/policies/documents",
        json={
            "title": "仅限金卡会员的补偿政策",
            "version": "v7.7",
            "content": "仅 gold 会员可享受物流停滞补偿，其余等级按基础标准执行。",
            "scope": {"member_level": "gold"},
        },
        headers=auth["reviewer"],
    ).json()
    assert created["scope"] == {"member_level": "gold"}

    # 不传会员等级 → 维度未知，保守判不适用
    unknown = next(item for item in _policies(client, auth) if item["id"] == created["id"])
    assert unknown["usable"] is False
    assert "会员等级" in unknown["reason"]

    # 传 gold → 适用；传 silver → 不适用，并说明原因
    gold = next(
        item for item in _policies(client, auth, member_level="gold") if item["id"] == created["id"]
    )
    assert gold["usable"] is True, gold["reason"]

    silver = next(
        item
        for item in _policies(client, auth, member_level="silver")
        if item["id"] == created["id"]
    )
    assert silver["usable"] is False
    assert "会员等级" in silver["reason"]

    # 检索测试同样要吃这个维度（否则界面上的"检索测试"与清单会给出互相矛盾的结论）
    resp = client.post(
        "/api/v1/policies/search-test",
        json={"query": "物流停滞补偿标准", "member_level": "gold", "top_k": 5},
        headers=auth["ops"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["member_level"] == "gold"
    excluded = {item["version"] for item in body.get("excluded_policies") or []}
    assert "v7.7" not in excluded, "gold 会员下这条政策不该被排除"


def test_list_policies_at_matches_decision_time(client, auth):
    """清单回显的判定时点必须与真正用于判定的时点一致。

    回归背景：`list_policies` 内部用 `utcnow()` 判定，而接口回显 `datetime.now()`（本地时间）
    —— 界面上同一屏出现两个"现在"（17:24 / 09:24），看起来像 bug 也会把人带偏。
    """
    from datetime import datetime, timedelta, timezone

    resp = client.get("/api/v1/policies", headers=auth["ops"])
    assert resp.status_code == 200
    returned = datetime.fromisoformat(resp.json()["at"])
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(returned - now_utc) < timedelta(minutes=5), (
        f"回显时点 {returned} 与 UTC 现在 {now_utc} 相差过大（多半又用回了本地时间）"
    )


def test_ticket_analysis_does_not_cite_expired_policy(client, auth):
    """端到端：物流停滞工单的引用里不得出现过期政策文档。"""
    ticket = client.post(
        "/api/v1/tickets",
        json={
            "subject": "物流停滞要补偿",
            "content": "包裹物流停滞超过 48 小时没有更新，请按规定补偿。",
            "external_id": "POL-TEST-0001",
            "order_id": "TB202609240655",
        },
        headers=auth["ops"],
    ).json()
    assert client.post(f"/api/v1/tickets/{ticket['id']}/analyze", headers=auth["ops"]).status_code == 202

    detail = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"]).json()
    recommendation = detail["recommendation"]
    assert recommendation is not None
    refs = recommendation.get("policy_refs") or []
    versions = {str(ref.get("version")) for ref in refs}
    assert "v1.0" not in versions, f"不得引用已失效政策：{refs}"
    assert "v4.0" not in versions, f"不得引用未生效政策：{refs}"

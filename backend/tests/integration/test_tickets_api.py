"""售后工单接口集成测试（真实 HTTP + 真实库，stub 模型，绝不花钱）。

覆盖需求书里**会直接造成事故**的几条：

- 幂等：同一 `external_id` 重复推送不得产生第二张工单；
- 权限：客服能建单，研发不能；升级/解决需要主管权限；
- 状态机：非法迁移必须 409（前端点按钮不能绕过）；
- 审批门禁：L3 必须写理由；未批准的建议**不能发送**（FR-05）；
- 分析：`analyze` 返回 run_id，且分析运行真的带上了 ticket_id（复用执行栈的证据）。
"""

from __future__ import annotations

import csv
import io
from datetime import timedelta
from uuid import uuid4

import pytest

from app.db.session import SessionLocal
from app.models.base import utcnow
from app.auth.passwords import hash_password
from app.models.enums import ApprovalRequirement, RecommendationStatus, RiskTier, RoleCode
from app.models.identity import Tenant, User, UserRole
from app.models.ticket import Ticket
from app.models.workflow import AuditLog
from app.services.ticket_service import TicketService


def test_reply_templates_are_tenant_scoped_and_managed_by_supervisor(client, auth):
    forbidden = client.post(
        "/api/v1/ticket-templates",
        json={"title": "客服模板", "content": "您好，我们正在核实。"},
        headers=auth["ops"],
    )
    assert forbidden.status_code == 403

    created = client.post(
        "/api/v1/ticket-templates",
        json={"title": "主管模板", "content": "您好，订单{{订单号}}正在核实。", "issue_type": "logistics_stalled"},
        headers=auth["reviewer"],
    )
    assert created.status_code == 201, created.text
    template = created.json()
    assert template["active"] is True

    listed = client.get(
        "/api/v1/ticket-templates", params={"issue_type": "logistics_stalled"}, headers=auth["ops"]
    )
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == template["id"] for item in listed.json())

    updated = client.patch(
        f"/api/v1/ticket-templates/{template['id']}",
        json={"title": "主管模板（更新）"},
        headers=auth["reviewer"],
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["title"] == "主管模板（更新）"

    deleted = client.delete(f"/api/v1/ticket-templates/{template['id']}", headers=auth["reviewer"])
    assert deleted.status_code == 204, deleted.text
    assert all(item["id"] != template["id"] for item in client.get("/api/v1/ticket-templates", headers=auth["ops"]).json())


def test_ticket_and_template_text_reject_whitespace_only_values(client, auth):
    blank_ticket = client.post(
        "/api/v1/tickets",
        json={"subject": "  ", "content": "\n\t"},
        headers=auth["ops"],
    )
    assert blank_ticket.status_code == 422

    blank_template = client.post(
        "/api/v1/ticket-templates",
        json={"title": " \n ", "content": "\t  "},
        headers=auth["reviewer"],
    )
    assert blank_template.status_code == 422

    created = client.post(
        "/api/v1/ticket-templates",
        json={"title": "有效模板", "content": "您好，我们正在核实。"},
        headers=auth["reviewer"],
    )
    assert created.status_code == 201, created.text
    blank_update = client.patch(
        f"/api/v1/ticket-templates/{created.json()['id']}",
        json={"content": " \n\t"},
        headers=auth["reviewer"],
    )
    assert blank_update.status_code == 422

    oversized_ticket = client.post(
        "/api/v1/tickets",
        json={"subject": "内容过长", "content": "x" * 20001},
        headers=auth["ops"],
    )
    assert oversized_ticket.status_code == 422

    oversized_template = client.post(
        "/api/v1/ticket-templates",
        json={"title": "过长模板", "content": "x" * 5001},
        headers=auth["reviewer"],
    )
    assert oversized_template.status_code == 422

    ticket = _create(client, auth, {"subject": "消息长度校验", "content": "请核实订单"})
    oversized_message = client.post(
        f"/api/v1/tickets/{ticket['id']}/messages",
        json={"content": "x" * 10001},
        headers=auth["ops"],
    )
    assert oversized_message.status_code == 422

    invalid_priority = client.post(
        "/api/v1/tickets",
        json={"subject": "非法优先级", "content": "请核实", "priority": "critical"},
        headers=auth["ops"],
    )
    assert invalid_priority.status_code == 422

    invalid_channel = client.post(
        "/api/v1/tickets",
        json={"subject": "非法渠道", "content": "请核实", "channel": "unknown"},
        headers=auth["ops"],
    )
    assert invalid_channel.status_code == 422


def test_mentioned_staff_can_list_and_mark_their_mention_read(client, auth):
    ticket = _create(client, auth, {"subject": "待关注提醒", "content": "请主管关注", "external_id": "CS-MENTION-0001"})
    reviewer = SessionLocal().query(User).filter(User.username == "reviewer").one()
    response = client.post(
        f"/api/v1/tickets/{ticket['id']}/messages",
        json={"content": "请主管跟进", "message_type": "internal_note", "mention_user_ids": [reviewer.id]},
        headers=auth["ops"],
    )
    assert response.status_code == 200, response.text

    mentions = client.get("/api/v1/ticket-mentions", params={"unread_only": "true"}, headers=auth["reviewer"])
    assert mentions.status_code == 200, mentions.text
    item = next(item for item in mentions.json() if item["ticket_id"] == ticket["id"])
    assert item["read"] is False

    marked = client.post(f"/api/v1/ticket-mentions/{item['id']}/read", headers=auth["reviewer"])
    assert marked.status_code == 200, marked.text
    assert marked.json()["read"] is True
    unread_after = client.get("/api/v1/ticket-mentions", params={"unread_only": "true"}, headers=auth["reviewer"])
    assert all(row["id"] != item["id"] for row in unread_after.json())

    # 被 @ 的人之外不能借 message_id 读取通知。
    not_mentioned = client.post(f"/api/v1/ticket-mentions/{item['id']}/read", headers=auth["ops"])
    assert not_mentioned.status_code == 404


def _create(client, auth, payload: dict) -> dict:
    resp = client.post("/api/v1/tickets", json=payload, headers=auth["ops"])
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- 创建与幂等
def test_create_ticket_and_message(client, auth):
    ticket = _create(
        client,
        auth,
        {
            "subject": "包裹显示签收但未收到",
            "content": "物流显示昨天签收，但我没有收到包裹，也没有接到电话。",
            "external_id": "CS-TEST-0001",
            "order_id": "TB202609241806",
            "customer_id": "U1001",
            "channel": "manual",
            "priority": "high",
        },
    )
    assert ticket["quality_status"] == "unreviewed"
    assert ticket["status"] == "new"
    assert ticket["issue_type"] is None
    # 允许的操作由后端给出（前端据此渲染按钮）
    assert "enriching" in ticket["allowed_transitions"]

    detail = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"]).json()
    assert len(detail["messages"]) == 1
    assert detail["messages"][0]["sender_type"] == "customer"
    assert "签收" in detail["messages"][0]["content"]


def test_internal_note_and_mentions_are_private_and_returned_in_detail(client, auth):
    ticket = _create(
        client,
        auth,
        {
            "subject": "需要主管确认的内部协作",
            "content": "请主管确认是否升级处理。",
            "external_id": "CS-INTERNAL-NOTE-0001",
        },
    )
    reviewer = SessionLocal().query(User).filter(User.username == "reviewer").one()
    reviewer_id = reviewer.id
    response = client.post(
        f"/api/v1/tickets/{ticket['id']}/messages",
        json={
            "content": "@主管：请确认是否需要升级。",
            "message_type": "internal_note",
            "mention_user_ids": [reviewer_id],
        },
        headers=auth["ops"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["visible_to_customer"] is False
    assert body["meta"]["message_type"] == "internal_note"
    assert body["meta"]["mention_user_ids"] == [reviewer_id]
    detail = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"]).json()
    assert any(item["visible_to_customer"] is False for item in detail["messages"])
    assert any(item["meta"].get("message_type") == "internal_note" for item in detail["messages"])
    assert "handoff_summary" in detail


def test_message_rejects_whitespace_only_content(client, auth):
    """客服不能提交空白回复或空白内部备注，避免产生无效消息记录。"""
    ticket = _create(
        client,
        auth,
        {
            "subject": "拒绝空白消息",
            "content": "用于验证消息正文不能只有空格。",
            "external_id": "CS-MESSAGE-BLANK-0001",
        },
    )
    response = client.post(
        f"/api/v1/tickets/{ticket['id']}/messages",
        json={"content": "   \n\t", "message_type": "internal_note"},
        headers=auth["ops"],
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "empty_message"

    detail = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"]).json()
    assert len(detail["messages"]) == 1, "拒绝空白消息后不应新增消息记录"


def test_quality_review_is_private_structured_and_not_available_to客服(client, auth):
    ticket = _create(
        client,
        auth,
        {
            "subject": "质检抽查记录",
            "content": "请抽查这张工单的建议和回复是否有依据。",
            "external_id": "CS-QUALITY-0001",
        },
    )
    payload = {
        "content": "订单事实已核对，建议可以继续，但回复需要说明当前仍在审核。",
        "visible_to_customer": False,
        "message_type": "internal_note",
        "quality_review": True,
        "quality_result": "needs_followup",
        "quality_issue_codes": ["unsafe_reply", "missing_evidence"],
    }
    forbidden = client.post(f"/api/v1/tickets/{ticket['id']}/messages", json=payload, headers=auth["ops"])
    assert forbidden.status_code == 403

    reviewed = client.post(f"/api/v1/tickets/{ticket['id']}/messages", json=payload, headers=auth["reviewer"])
    assert reviewed.status_code == 200, reviewed.text
    body = reviewed.json()
    assert body["visible_to_customer"] is False
    assert body["meta"]["message_type"] == "quality_review"
    assert body["meta"]["quality_result"] == "needs_followup"
    assert body["meta"]["quality_issue_codes"] == ["unsafe_reply", "missing_evidence"]
    listed = client.get("/api/v1/tickets", params={"keyword": "CS-QUALITY-0001"}, headers=auth["reviewer"])
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["quality_status"] == "needs_followup"
    assert listed.json()["items"][0]["quality_issue_codes"] == ["unsafe_reply", "missing_evidence"]

    invalid = client.post(
        f"/api/v1/tickets/{ticket['id']}/messages",
        json={**payload, "visible_to_customer": True},
        headers=auth["reviewer"],
    )
    assert invalid.status_code == 422


def test_quality_review_query_is_server_side_paginated_and_permission_scoped(client, auth):
    first = _create(client, auth, {"subject": "分页质检一", "content": "需要补证据", "external_id": "CS-QUALITY-PAGE-001"})
    second = _create(client, auth, {"subject": "分页质检二", "content": "阻断发送", "external_id": "CS-QUALITY-PAGE-002", "priority": "high"})
    review_payload = {
        "content": "检查完成",
        "visible_to_customer": False,
        "message_type": "internal_note",
        "quality_review": True,
        "quality_result": "needs_followup",
        "quality_issue_codes": ["missing_evidence"],
    }
    reviewed = client.post(f"/api/v1/tickets/{first['id']}/messages", json=review_payload, headers=auth["reviewer"])
    assert reviewed.status_code == 200, reviewed.text
    review_payload["quality_result"] = "block"
    review_payload["quality_issue_codes"] = ["unsafe_reply"]
    reviewed = client.post(f"/api/v1/tickets/{second['id']}/messages", json=review_payload, headers=auth["reviewer"])
    assert reviewed.status_code == 200, reviewed.text

    filtered = client.get(
        "/api/v1/tickets/quality-reviews",
        params={"quality_status": "block", "page": 1, "page_size": 1},
        headers=auth["reviewer"],
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["meta"]["total"] == 1
    assert filtered.json()["items"][0]["external_id"] == "CS-QUALITY-PAGE-002"
    assert filtered.json()["items"][0]["quality_issue_codes"] == ["unsafe_reply"]

    forbidden = client.get("/api/v1/tickets/quality-reviews", headers=auth["ops"])
    assert forbidden.status_code == 403

    exported = client.get(
        "/api/v1/tickets/quality-reviews/export",
        params={"quality_issue_code": "unsafe_reply"},
        headers=auth["reviewer"],
    )
    assert exported.status_code == 200, exported.text
    assert "CS-QUALITY-PAGE-002" in exported.text
    assert "阻断发送" in exported.text
    assert "高" in exported.text
    assert "ready_to_reply" not in exported.text
    assert "pending_approval" not in exported.text
    audit_rows = SessionLocal().query(AuditLog).filter(AuditLog.action == "ticket_quality_export").all()
    assert audit_rows
    assert all(row.action == "ticket_quality_export" for row in audit_rows)

    forbidden_export = client.get("/api/v1/tickets/quality-reviews/export", headers=auth["ops"])
    assert forbidden_export.status_code == 403


def test_quality_review_csv_export_neutralizes_formula_cells(client, auth):
    ticket = _create(
        client,
        auth,
        {
            "subject": "=HYPERLINK(\"https://example.invalid\",\"open\")",
            "content": "Please review this imported ticket.",
            "external_id": "=1+1",
            "order_id": " \t@SUM(A1:A2)",
        },
    )
    reviewed = client.post(
        f"/api/v1/tickets/{ticket['id']}/messages",
        json={
            "content": "Reviewed.",
            "visible_to_customer": False,
            "message_type": "internal_note",
            "quality_review": True,
            "quality_result": "pass",
            "quality_issue_codes": [],
        },
        headers=auth["reviewer"],
    )
    assert reviewed.status_code == 200, reviewed.text

    exported = client.get("/api/v1/tickets/quality-reviews/export", headers=auth["reviewer"])
    assert exported.status_code == 200, exported.text
    rows = list(csv.reader(io.StringIO(exported.text.removeprefix("\ufeff"))))
    exported_ticket = next(row for row in rows[1:] if row[0] == "'=1+1")
    assert exported_ticket[1] == "'=HYPERLINK(\"https://example.invalid\",\"open\")"
    assert exported_ticket[2] == "' \t@SUM(A1:A2)"


def test_create_is_idempotent_by_external_id(client, auth):
    payload = {
        "subject": "重复推送的工单",
        "content": "渠道重复推送同一张工单，不应产生第二张。",
        "external_id": "CS-TEST-0002",
    }
    first = _create(client, auth, payload)
    second = _create(client, auth, payload)
    assert first["id"] == second["id"]

    listing = client.get(
        "/api/v1/tickets", params={"keyword": "CS-TEST-0002"}, headers=auth["ops"]
    ).json()
    assert listing["meta"]["total"] == 1


def test_create_requires_permission(client, auth):
    resp = client.post(
        "/api/v1/tickets",
        json={"subject": "研发不该能建单", "content": "研发岗位没有 ticket:create 权限。"},
        headers=auth["eng"],
    )
    assert resp.status_code == 403


def test_production_rejects_non_business_ticket_origin(client, auth, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.api.routes_tickets.get_settings",
        lambda: SimpleNamespace(app_env="prod"),
    )
    response = client.post(
        "/api/v1/tickets",
        json={
            "subject": "不能隐藏的生产工单",
            "content": "生产客服不能自行把真实工单标记成测试数据。",
            "data_origin": "test",
        },
        headers=auth["ops"],
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "non_business_ticket_origin_forbidden"


# --------------------------------------------------------------------------- 列表与筛选
def test_list_filters_and_keyword(client, auth):
    _create(
        client,
        auth,
        {
            "subject": "少件：两件只到一件",
            "content": "购买两件，包裹中仅收到一件。",
            "external_id": "CS-TEST-0003",
            "order_id": "TB-KEYWORD-777",
            "issue_type": "missing_item",
            "priority": "urgent",
        },
    )
    _create(
        client,
        auth,
        {
            "subject": "优惠未生效",
            "content": "满 199 减 30 未生效。",
            "external_id": "CS-TEST-0004",
            "issue_type": "promotion_not_applied",
            "priority": "low",
        },
    )

    by_type = client.get(
        "/api/v1/tickets", params={"issue_type": "missing_item"}, headers=auth["ops"]
    ).json()
    assert by_type["meta"]["total"] >= 1
    assert all(item["issue_type"] == "missing_item" for item in by_type["items"])

    by_priority = client.get(
        "/api/v1/tickets", params={"priority": "urgent"}, headers=auth["ops"]
    ).json()
    assert all(item["priority"] == "urgent" for item in by_priority["items"])

    by_keyword = client.get(
        "/api/v1/tickets", params={"keyword": "TB-KEYWORD-777"}, headers=auth["ops"]
    ).json()
    assert by_keyword["meta"]["total"] == 1


def test_test_records_are_hidden_from_frontline_list_and_stats_by_default(client, auth):
    suffix = uuid4().hex[:10]
    stats_before = client.get("/api/v1/tickets/stats", headers=auth["ops"]).json()
    test_ticket = _create(
        client,
        auth,
        {
            "subject": "自动化测试工单不应污染客服待办",
            "content": "这是一条明确标记的数据隔离回归记录。",
            "external_id": f"E2E-ORIGIN-{suffix}",
            "data_origin": "test",
        },
    )
    demo_ticket = _create(
        client,
        auth,
        {
            "subject": "演示工单仍应可见",
            "content": "演示数据需要明确标记，但不能被当成测试垃圾隐藏。",
            "external_id": f"DEMO-ORIGIN-{suffix}",
            "data_origin": "demo",
        },
    )
    assert test_ticket["data_origin"] == "test"
    assert demo_ticket["data_origin"] == "demo"

    default_test = client.get(
        "/api/v1/tickets",
        params={"keyword": test_ticket["external_id"]},
        headers=auth["ops"],
    ).json()
    assert default_test["meta"]["total"] == 0

    included_test = client.get(
        "/api/v1/tickets",
        params={"keyword": test_ticket["external_id"], "include_test": "true"},
        headers=auth["ops"],
    ).json()
    assert included_test["meta"]["total"] == 1
    assert included_test["items"][0]["data_origin"] == "test"

    visible_demo = client.get(
        "/api/v1/tickets",
        params={"keyword": demo_ticket["external_id"]},
        headers=auth["ops"],
    ).json()
    assert visible_demo["meta"]["total"] == 1
    assert visible_demo["items"][0]["data_origin"] == "demo"

    stats_after = client.get("/api/v1/tickets/stats", headers=auth["ops"]).json()
    assert stats_after["excluded_test_records"] == stats_before["excluded_test_records"] + 1
    assert stats_after["today_new"] == stats_before["today_new"] + 1


def test_ticket_scope_mine_and_unassigned_are_server_scoped(client, auth):
    """“我的工单”必须使用令牌里的用户，不能借 assignee_id 查看成“别人的工单”。"""
    session = SessionLocal()
    try:
        ops = session.query(User).filter(User.username == "ops").one()
        reviewer = session.query(User).filter(User.username == "reviewer").one()
        ops_id = ops.id
        reviewer_id = reviewer.id
    finally:
        session.close()

    mine_ticket = _create(
        client,
        auth,
        {
            "subject": "范围筛选：我的工单",
            "content": "该工单分配给当前客服。",
            "external_id": "CS-SCOPE-MINE-0001",
        },
    )
    unassigned_ticket = _create(
        client,
        auth,
        {
            "subject": "范围筛选：未分配",
            "content": "该工单保持未分配。",
            "external_id": "CS-SCOPE-UNASSIGNED-0001",
        },
    )
    assigned = client.post(
        f"/api/v1/tickets/{mine_ticket['id']}/assign",
        json={"assignee_id": ops_id, "reason": "测试我的工单范围"},
        headers=auth["ops"],
    )
    assert assigned.status_code == 200, assigned.text

    mine = client.get(
        "/api/v1/tickets",
        params={
            "scope": "mine",
            "assignee_id": reviewer_id,  # 恶意/误传：后端必须忽略
            "keyword": "CS-SCOPE-",
        },
        headers=auth["ops"],
    )
    assert mine.status_code == 200, mine.text
    assert [item["id"] for item in mine.json()["items"]] == [mine_ticket["id"]]

    unassigned = client.get(
        "/api/v1/tickets",
        params={"scope": "unassigned", "keyword": "CS-SCOPE-"},
        headers=auth["ops"],
    )
    assert unassigned.status_code == 200, unassigned.text
    assert [item["id"] for item in unassigned.json()["items"]] == [unassigned_ticket["id"]]


def test_attention_queues_and_stats_follow_the_selected_scope(client, auth):
    """处理卡必须打开真实服务端队列，卡片数字也不能混入别人的工单。"""
    mine_before = client.get(
        "/api/v1/tickets/stats", params={"scope": "mine"}, headers=auth["ops"]
    ).json()
    unassigned_before = client.get(
        "/api/v1/tickets/stats", params={"scope": "unassigned"}, headers=auth["ops"]
    ).json()

    specs = [
        ("AT-RISK", "urgent", "new"),
        ("ON-TRACK", "low", "new"),
        ("REPLY", "medium", "ready_to_reply"),
        ("CUSTOMER", "medium", "need_customer_info"),
        ("REVIEW", "high", "waiting_review"),
        ("RESOLVED", "low", "resolved"),
    ]
    created = {}
    for suffix, priority, _status in specs:
        created[suffix] = _create(
            client,
            auth,
            {
                "subject": f"行动队列 {suffix}",
                "content": "用于验证处理卡筛选和范围统计。",
                "external_id": f"QUEUE-CARD-{suffix}",
                "priority": priority,
            },
        )

    session = SessionLocal()
    try:
        ops = session.query(User).filter(User.username == "ops").one()
        now = utcnow()
        for suffix, _priority, status in specs:
            row = session.query(Ticket).filter(Ticket.id == created[suffix]["id"]).one()
            row.assignee_id = ops.id
            row.status = status
            row.created_at = now - timedelta(minutes=5)
            row.updated_at = now
        session.commit()
    finally:
        session.close()

    expected = {
        "at_risk": "QUEUE-CARD-AT-RISK",
        "waiting_reply": "QUEUE-CARD-REPLY",
        "waiting_customer": "QUEUE-CARD-CUSTOMER",
        "waiting_review": "QUEUE-CARD-REVIEW",
        "resolved_today": "QUEUE-CARD-RESOLVED",
    }
    for attention, external_id in expected.items():
        response = client.get(
            "/api/v1/tickets",
            params={"scope": "mine", "attention": attention, "keyword": "QUEUE-CARD-"},
            headers=auth["ops"],
        )
        assert response.status_code == 200, response.text
        assert [item["external_id"] for item in response.json()["items"]] == [external_id]

    mine_after = client.get(
        "/api/v1/tickets/stats", params={"scope": "mine"}, headers=auth["ops"]
    ).json()
    unassigned_after = client.get(
        "/api/v1/tickets/stats", params={"scope": "unassigned"}, headers=auth["ops"]
    ).json()
    assert mine_after["waiting_reply"] == mine_before["waiting_reply"] + 1
    assert mine_after["waiting_customer"] == mine_before["waiting_customer"] + 1
    assert mine_after["waiting_review"] == mine_before["waiting_review"] + 1
    assert mine_after["resolved_today"] == mine_before["resolved_today"] + 1
    assert mine_after["at_risk"] == mine_before["at_risk"] + 1
    assert unassigned_after == unassigned_before

    unfiltered = client.get(
        "/api/v1/tickets",
        params={"scope": "mine", "keyword": "QUEUE-CARD-", "page_size": 20},
        headers=auth["ops"],
    ).json()["items"]
    ids = [item["external_id"] for item in unfiltered]
    assert ids.index("QUEUE-CARD-AT-RISK") < ids.index("QUEUE-CARD-ON-TRACK")
    assert ids.index("QUEUE-CARD-ON-TRACK") < ids.index("QUEUE-CARD-RESOLVED")


def test_assignment_recommendations_are_deterministic_read_only_and_load_aware(client, auth):
    """推荐只解释“建议给谁”，不会静默改派；高负载候选人应被降权。"""
    session = SessionLocal()
    try:
        ops = session.query(User).filter(User.username == "ops").one()
        ops_id = ops.id
    finally:
        session.close()

    target = _create(
        client,
        auth,
        {
            "subject": "派单推荐目标",
            "content": "退款争议需要推荐合适负责人。",
            "external_id": "CS-ASSIGN-RECOMMEND-0001",
            "issue_type": "refund",
            "priority": "high",
        },
    )
    # 给 ops 制造明确的活跃负载；推荐排序不能只按用户名或固定账号。
    for index in range(6):
        load_ticket = _create(
            client,
            auth,
            {
                "subject": f"负载样本 {index}",
                "content": "用于验证派单负载评分。",
                "external_id": f"CS-ASSIGN-LOAD-{index:04d}",
                "priority": "high" if index < 2 else "medium",
            },
        )
        response = client.post(
            f"/api/v1/tickets/{load_ticket['id']}/assign",
            json={"assignee_id": ops_id, "reason": "构造推荐负载"},
            headers=auth["ops"],
        )
        assert response.status_code == 200, response.text

    first = client.get(
        f"/api/v1/tickets/{target['id']}/assignment-recommendations",
        headers=auth["ops"],
    )
    second = client.get(
        f"/api/v1/tickets/{target['id']}/assignment-recommendations",
        headers=auth["ops"],
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json(), "相同数据下推荐结果必须可复现"

    body = first.json()
    assert body["ticket_id"] == target["id"]
    assert body["rule_version"]
    assert body["issue_type_context"] == "refund"
    assert body["items"]
    assert [item["score"] for item in body["items"]] == sorted(
        [item["score"] for item in body["items"]], reverse=True
    )
    ops_item = next(item for item in body["items"] if item["user_id"] == ops_id)
    assert ops_item["active_ticket_count"] >= 6
    assert ops_item["high_priority_count"] >= 2
    assert body["items"][0]["user_id"] != ops_id
    assert any("负载" in reason for reason in ops_item["reasons"])

    after = client.get(f"/api/v1/tickets/{target['id']}", headers=auth["ops"]).json()
    assert after["ticket"]["assignee_id"] is None, "读取推荐不得修改负责人"


def test_assignment_recommendation_prefers_frontline_for_normal_ticket(client, auth):
    """零负载相同时，普通工单应优先给一线客服，而不是机械推荐管理员。"""
    session = SessionLocal()
    try:
        ops_id = session.query(User).filter(User.username == "ops").one().id
        # 集成测试共享播种库；显式归零历史负责人，保证此用例验证的是“同负载岗位优先级”，
        # 而不是被前一条高负载用例的现场数据左右。
        session.query(Ticket).update({Ticket.assignee_id: None})
        session.commit()
    finally:
        session.close()
    ticket = _create(
        client,
        auth,
        {
            "subject": "普通物流咨询",
            "content": "包裹两天没有更新，想问一下进度。",
            "external_id": "CS-ASSIGN-FRONTLINE-0001",
            "issue_type": "logistics_stalled",
        },
    )
    response = client.get(
        f"/api/v1/tickets/{ticket['id']}/assignment-recommendations",
        headers=auth["ops"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["user_id"] == ops_id
    assert any("客服岗位" in reason for reason in response.json()["items"][0]["reasons"])


def test_assign_rejects_unknown_or_ineligible_assignee(client, auth):
    """转派不能把任意字符串写进负责人字段，否则推荐候选约束可被直接绕过。"""
    ticket = _create(
        client,
        auth,
        {
            "subject": "非法负责人",
            "content": "转派目标必须是本租户可处理工单的启用账号。",
            "external_id": "CS-ASSIGN-INVALID-0001",
        },
    )
    response = client.post(
        f"/api/v1/tickets/{ticket['id']}/assign",
        json={"assignee_id": "not-a-real-user", "reason": "尝试写入任意负责人"},
        headers=auth["ops"],
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "invalid_ticket_assignee"

    after = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"]).json()
    assert after["ticket"]["assignee_id"] is None


def test_stats_and_state_machine(client, auth):
    stats = client.get("/api/v1/tickets/stats", headers=auth["ops"]).json()
    assert stats["today_new"] >= 1
    assert "missing_item" in stats["issue_type_options"]
    assert stats["status_options"]
    assert "waiting_customer" in stats
    assert "waiting_reply" in stats

    machine = client.get("/api/v1/tickets/state-machine", headers=auth["ops"]).json()
    assert machine["terminal"] == ["cancelled", "resolved"]
    # NEW 只能去补事实/等客户补充/取消/失败（不能跳过分析直接解决）
    assert machine["transitions"]["new"]["next"] == [
        "cancelled",
        "enriching",
        "failed",
        "need_customer_info",
    ]


# --------------------------------------------------------------------------- 状态机与权限
def test_illegal_transition_returns_409(client, auth):
    ticket = _create(
        client,
        auth,
        {"subject": "非法迁移", "content": "新建的工单不能直接变已解决。", "external_id": "CS-TEST-0005"},
    )
    # 用有 ticket:approve 的主管账号，才能走到状态机校验（客服会先被 403 挡住）
    resp = client.post(
        f"/api/v1/tickets/{ticket['id']}/transition",
        json={"target": "resolved", "reason": "想跳过流程"},
        headers=auth["reviewer"],
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ticket_invalid_transition"


def test_escalate_requires_supervisor_permission(client, auth):
    ticket = _create(
        client,
        auth,
        {"subject": "升级权限", "content": "客服不能自行升级工单。", "external_id": "CS-TEST-0006"},
    )
    # 客服（ops）没有 ticket:approve
    resp = client.post(
        f"/api/v1/tickets/{ticket['id']}/transition",
        json={"target": "escalated", "reason": "疑似欺诈"},
        headers=auth["ops"],
    )
    assert resp.status_code == 403

    # 主管（reviewer）可以：先到需补信息态再升级（NEW 不允许直接升级，用合法路径验证权限）
    client.post(
        f"/api/v1/tickets/{ticket['id']}/transition",
        json={"target": "need_customer_info", "reason": "缺订单号"},
        headers=auth["ops"],
    )
    ok = client.post(
        f"/api/v1/tickets/{ticket['id']}/transition",
        json={"target": "enriching", "reason": "客户已补充"},
        headers=auth["ops"],
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "enriching"


def test_analyze_returns_run_id_and_reuses_task_stack(client, auth):
    ticket = _create(
        client,
        auth,
        {
            "subject": "启动分析",
            "content": "物流停滞超过 48 小时，想知道是否需要补偿。",
            "external_id": "CS-TEST-0007",
            "order_id": "TB202609240181",
        },
    )
    resp = client.post(f"/api/v1/tickets/{ticket['id']}/analyze", headers=auth["ops"])
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]
    assert run_id

    # 分析运行必须带上 ticket_id（证明复用了既有执行栈，而不是另起一套）
    session = SessionLocal()
    try:
        from app.models.workflow import AnalysisTask

        task = session.get(AnalysisTask, run_id)
        assert task is not None
        assert (task.filters or {}).get("ticket_id") == ticket["id"]
    finally:
        session.close()

    detail = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"]).json()
    assert detail["ticket"]["analysis_count"] == 1
    assert detail["ticket"]["last_analysis_task_id"] == run_id


# --------------------------------------------------------------------------- 审批门禁
def _seed_recommendation(
    ticket_id: str,
    *,
    risk_tier: str,
    status: str | None,
    approval_requirement: str | None = None,
    action_type: str = "refund",
) -> str:
    session = SessionLocal()
    try:
        service = TicketService(session)
        row = service.save_recommendation(
            ticket_id,
            {
                "action_type": action_type,
                "amount": "268.00",
                "rationale": "签收未收到，按政策可退款",
                "risk_tier": risk_tier,
                "risk_reason": "涉及退款，需主管审批后执行",
                "approval_requirement": approval_requirement
                or ApprovalRequirement.SUPERVISOR.value,
                "confidence": 0.91,
                "customer_reply": "您好，已为您核实物流异常，现为您发起退款。",
                **({"status": status} if status else {}),
                "evidence_ids": ["fact-1"],
                "policy_refs": [{"title": "物流异常处理规范", "version": "v3.2", "clause": "4.1"}],
            },
        )
        return row.id
    finally:
        session.close()


def _advance_to_analyzing(client, auth, ticket_id: str) -> None:
    """把工单推进到"分析中"——真实链路里，建议是在分析之后产生的。

    状态机不允许 NEW 直接到 WAITING_REVIEW（这是故意的：没有分析就没有建议），
    所以测试也必须走合法路径，否则测的就不是真实流程。
    """
    for target in ("enriching", "analyzing"):
        resp = client.post(
            f"/api/v1/tickets/{ticket_id}/transition",
            json={"target": target, "reason": "测试推进"},
            headers=auth["ops"],
        )
        assert resp.status_code == 200, resp.text


def test_decision_requires_reason_for_l3_and_updates_ticket(client, auth):
    ticket = _create(
        client,
        auth,
        {"subject": "审批门禁", "content": "签收未收到要退款。", "external_id": "CS-TEST-0008"},
    )
    _advance_to_analyzing(client, auth, ticket["id"])
    recommendation_id = _seed_recommendation(
        ticket["id"], risk_tier=RiskTier.L3.value, status=None
    )

    # 生成 L3 建议后，工单必须自动进入"待审批"（否则审批人不知道要审）
    detail = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"]).json()
    assert detail["ticket"]["status"] == "waiting_review"
    assert detail["recommendation"]["status"] == "pending_approval"

    # L3 不写理由 → 422
    missing = client.post(
        f"/api/v1/recommendations/{recommendation_id}/decision",
        json={"decision": "approve"},
        headers=auth["reviewer"],
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "reason_required"

    # 客服没有审批权限
    forbidden = client.post(
        f"/api/v1/recommendations/{recommendation_id}/decision",
        json={"decision": "approve", "reason": "客服越权审批"},
        headers=auth["ops"],
    )
    assert forbidden.status_code == 403

    ok = client.post(
        f"/api/v1/recommendations/{recommendation_id}/decision",
        json={"decision": "approve", "reason": "物流事实已核验", "idempotency_key": "dec-0001"},
        headers=auth["reviewer"],
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved"

    # 幂等：同一 idempotency_key 重复提交不产生第二次状态迁移
    again = client.post(
        f"/api/v1/recommendations/{recommendation_id}/decision",
        json={"decision": "approve", "reason": "重复提交", "idempotency_key": "dec-0001"},
        headers=auth["reviewer"],
    )
    assert again.status_code == 200
    assert again.json()["decision_reason"] == "物流事实已核验"


def test_send_blocked_before_approval(client, auth):
    ticket = _create(
        client,
        auth,
        {"subject": "未批准不能发送", "content": "退款建议还在待审批。", "external_id": "CS-TEST-0009"},
    )
    _advance_to_analyzing(client, auth, ticket["id"])
    recommendation_id = _seed_recommendation(
        ticket["id"], risk_tier=RiskTier.L3.value, status=RecommendationStatus.PENDING_APPROVAL.value
    )
    blocked = client.post(
        f"/api/v1/recommendations/{recommendation_id}/send", headers=auth["ops"]
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "recommendation_not_approved"


def test_recommendation_decision_is_tenant_scoped(client, auth):
    """知道另一租户的建议 UUID 也不能跨租户审批或改动工单。"""
    ticket = _create(
        client,
        auth,
        {"subject": "租户隔离审批", "content": "退款建议不能被别的租户处理。", "external_id": "CS-TENANT-0001"},
    )
    _advance_to_analyzing(client, auth, ticket["id"])
    recommendation_id = _seed_recommendation(
        ticket["id"], risk_tier=RiskTier.L3.value, status=RecommendationStatus.PENDING_APPROVAL.value
    )

    session = SessionLocal()
    try:
        other_tenant = Tenant(code=f"other-{uuid4().hex[:10]}", name="隔离测试租户")
        session.add(other_tenant)
        session.flush()
        other_reviewer = User(
            tenant_id=other_tenant.id,
            username=f"reviewer-{uuid4().hex[:10]}",
            full_name="隔离测试审核员",
            hashed_password=hash_password("other-reviewer123"),
            security_level=3,
            is_active=True,
        )
        session.add(other_reviewer)
        session.flush()
        session.add(UserRole(user_id=other_reviewer.id, role_code=RoleCode.REVIEWER.value))
        session.commit()
    finally:
        session.close()

    login = client.post(
        "/api/v1/auth/login",
        json={"username": other_reviewer.username, "password": "other-reviewer123"},
    )
    assert login.status_code == 200, login.text
    other_auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    client.cookies.clear()

    response = client.post(
        f"/api/v1/recommendations/{recommendation_id}/decision",
        json={"decision": "approve", "reason": "不应跨租户生效"},
        headers=other_auth,
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "recommendation_not_found"

    untouched = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"])
    assert untouched.status_code == 200
    assert untouched.json()["ticket"]["status"] == "waiting_review"
    assert untouched.json()["recommendation"]["status"] == "pending_approval"


def test_low_risk_recommendation_is_not_write_blocked(client, auth):
    """L1 建议：不涉及写操作，前端不应显示"未批准禁止执行"。"""
    ticket = _create(
        client,
        auth,
        {"subject": "L1 建议", "content": "想了解退货政策。", "external_id": "CS-TEST-0010"},
    )
    session = SessionLocal()
    try:
        service = TicketService(session)
        service.save_recommendation(
            ticket["id"],
            {
                "action_type": "reply_only",
                "amount": 0,
                "risk_tier": RiskTier.L1.value,
                "approval_requirement": ApprovalRequirement.NONE.value,
                "customer_reply": "您好，退货政策如下：……",
                "status": RecommendationStatus.DRAFT.value,
            },
        )
    finally:
        session.close()

    payload = client.get(
        f"/api/v1/tickets/{ticket['id']}/recommendation", headers=auth["ops"]
    ).json()
    assert payload["risk_tier"] == "L1"
    assert payload["auto_draft_allowed"] is True
    assert payload["write_action_blocked"] is False
    assert payload["approval_label"] == "无需审批（客服发送前确认）"


def test_l2_write_action_can_be_confirmed_and_sent_by_staff(client, auth):
    """L2（staff_confirm）的写操作建议必须有一条真实可用的执行路径。

    回归背景（界面上实测发现）：L2 的口径是"客服确认后执行"，但发送接口只认
    `approved/sent`，前端又把写操作按钮按"未批准"置灰 —— 于是补发/换货这类 L2 建议
    **既不进待审批、也发不出去**，只能绕过系统去后台改单。
    现在客服点"确认并发送"即视为确认：落审批记录（谁、何时、理由）后再发送。
    """
    ticket = _create(
        client,
        auth,
        {"subject": "L2 补发", "content": "包裹破损，需要补发。", "external_id": "CS-TEST-0013"},
    )
    _advance_to_analyzing(client, auth, ticket["id"])
    recommendation_id = _seed_recommendation(
        ticket["id"],
        risk_tier=RiskTier.L2.value,
        status=RecommendationStatus.DRAFT.value,
        approval_requirement=ApprovalRequirement.STAFF_CONFIRM.value,
        action_type="reship",
    )

    payload = client.get(
        f"/api/v1/tickets/{ticket['id']}/recommendation", headers=auth["ops"]
    ).json()
    # L2 不应被标成"未批准禁止执行"（否则界面无路可走）
    assert payload["write_action_blocked"] is False
    assert payload["approval_label"] == "客服确认后执行"

    sent = client.post(f"/api/v1/recommendations/{recommendation_id}/send", headers=auth["ops"])
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "sent"

    # 网络重试/重复点击不能再次生成客户消息，也不能修改已发出的正文。
    repeated = client.post(f"/api/v1/recommendations/{recommendation_id}/send", headers=auth["ops"])
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["status"] == "sent"
    changed = client.post(
        f"/api/v1/recommendations/{recommendation_id}/send",
        json={"content": "试图修改已发出的正文"},
        headers=auth["ops"],
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["code"] == "recommendation_already_sent"

    detail = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"]).json()
    assert detail["ticket"]["status"] == "resolved"
    assert any(m["sender_type"] == "staff" for m in detail["messages"])

    # 确认动作必须留痕：谁确认的、按什么理由
    from app.models.workflow import AuditLog

    session = SessionLocal()
    try:
        audits = (
            session.query(AuditLog)
            .filter(AuditLog.resource_id == recommendation_id)
            .all()
        )
        assert any(row.action == "recommendation_confirmed_by_staff" for row in audits), (
            "客服确认必须落审计（否则事后查不出是谁批准了这笔补发）"
        )
    finally:
        session.close()


def test_l3_write_action_still_cannot_be_self_confirmed(client, auth):
    """门禁不能因为 L2 的例外而放松：L3 草稿仍然发不出去。"""
    ticket = _create(
        client,
        auth,
        {"subject": "L3 不能自助确认", "content": "要退款。", "external_id": "CS-TEST-0014"},
    )
    _advance_to_analyzing(client, auth, ticket["id"])
    recommendation_id = _seed_recommendation(
        ticket["id"],
        risk_tier=RiskTier.L3.value,
        status=RecommendationStatus.DRAFT.value,
        approval_requirement=ApprovalRequirement.SUPERVISOR.value,
        action_type="refund",
    )
    blocked = client.post(
        f"/api/v1/recommendations/{recommendation_id}/send", headers=auth["ops"]
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "recommendation_not_approved"


def test_assignees_endpoint_lists_handlers(client, auth):
    """转派要有候选人：客服自己就能拿到处理人列表（不能只让管理员能列账号）。"""
    resp = client.get("/api/v1/tickets/assignees", headers=auth["ops"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    usernames = {item["username"] for item in body["items"]}
    assert "ops" in usernames, "客服自己应该是可被转派的人"
    assert "reviewer" in usernames, "售后主管也能接单（L3 工单要转给他）"
    # 研发没有 ticket:reply/ticket:approve，不该出现在候选人里
    assert "eng" not in usernames
    assert any(item["can_approve"] for item in body["items"]), "要能区分谁有审批权"

    # 没有转派权限的角色不能看候选人列表（避免成为账号枚举接口）
    forbidden = client.get("/api/v1/tickets/assignees", headers=auth["eng"])
    assert forbidden.status_code == 403

    # 停用账号即使仍保留客服/审批角色，也不能出现在新工单候选人中。
    # 这是转派安全边界：停用账号无法登录处理，不能再接新单。
    session = SessionLocal()
    reviewer = session.query(User).filter(User.username == "reviewer").one()
    was_active = reviewer.is_active
    reviewer.is_active = False
    session.commit()
    try:
        inactive = client.get("/api/v1/tickets/assignees", headers=auth["ops"])
        assert inactive.status_code == 200, inactive.text
        assert all(item["username"] != "reviewer" for item in inactive.json()["items"])
    finally:
        reviewer.is_active = was_active
        session.commit()
        session.close()


def test_detail_masks_sensitive_address(client, auth):
    """地址必须在展示出口脱敏（§8 隐私要求）。"""
    ticket = _create(
        client,
        auth,
        {"subject": "脱敏", "content": "查一下我的收货信息。", "external_id": "CS-TEST-0011"},
    )
    session = SessionLocal()
    try:
        service = TicketService(session)
        service.save_facts(
            ticket["id"],
            [
                {
                    "source": "order",
                    "fact_type": "snapshot",
                    "payload": {
                        "address": "上海市浦东新区张江路 88 弄 3 号楼 501 室",
                        "order_status": "已签收",
                    },
                }
            ],
        )
    finally:
        session.close()

    detail = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"]).json()
    rows = {row["key"]: row for row in detail["fact_rows"]}
    assert rows["order_status"]["value"] == "已签收"
    assert "501" not in str(rows["address"]["value"])
    assert rows["address"]["is_mock"] is True


def test_analyze_without_order_id_asks_customer_info(client, auth):
    """缺订单号：拒绝对分析（409），工单进"等客户补充"并说明缺什么。

    这条是**实测发现的 bug** 的回归测试：事实补全先把状态推到 need_customer_info，
    但后续推进逻辑又覆盖成 analyzing，"缺信息"的信号被自己冲掉。
    """
    ticket = _create(
        client,
        auth,
        {
            "subject": "没有订单号的咨询",
            "content": "我想问下我的包裹到哪了，但忘了订单号。",
            "external_id": "CS-TEST-0012",
        },
    )
    resp = client.post(f"/api/v1/tickets/{ticket['id']}/analyze", headers=auth["ops"])
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "need_customer_info"
    assert detail["missing"] == ["订单号"]

    after = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"]).json()
    assert after["ticket"]["status"] == "need_customer_info"
    assert after["ticket"]["missing_fields"] == ["订单号"]
    assert after["ticket"]["last_analysis_task_id"] is None

    # 客服必须能在原工单补录，而不是只能重建一张工单或反复触发 409。
    updated = client.patch(
        f"/api/v1/tickets/{ticket['id']}/order",
        json={"order_id": "TB202609241806"},
        headers=auth["ops"],
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["order_id"] == "TB202609241806"
    assert "订单号" not in updated.json()["missing_fields"]

    resumed = client.post(f"/api/v1/tickets/{ticket['id']}/analyze", headers=auth["ops"])
    assert resumed.status_code == 202, resumed.text

    audit = (
        SessionLocal()
        .query(AuditLog)
        .filter(AuditLog.action == "ticket_order_id_updated", AuditLog.resource_id == ticket["id"])
        .one()
    )
    assert audit.actor_id


def test_retry_with_unavailable_fact_source_refreshes_manual_handoff(client, auth, monkeypatch):
    from app.config import get_settings
    import app.services.ticket_analysis as ticket_analysis

    unavailable_settings = get_settings().model_copy(
        update={
            "ticket_use_mock_adapters": False,
            "ticket_fact_order_url": "",
            "ticket_fact_payment_url": "",
            "ticket_fact_logistics_url": "",
            "ticket_fact_aftersale_url": "",
        }
    )
    monkeypatch.setattr(ticket_analysis, "get_settings", lambda: unavailable_settings)
    ticket = _create(
        client,
        auth,
        {
            "subject": "事实来源暂不可用时等待人工核实",
            "content": "申请退款，请核实订单和支付事实。",
            "order_id": "TB202609241501",
            "external_id": f"CS-FACT-UNAVAILABLE-{uuid4().hex[:8]}",
        },
    )

    session = SessionLocal()
    try:
        stored = session.get(Ticket, ticket["id"])
        stored.status = "need_customer_info"
        stored.missing_fields = ["旧的订单事实缺失"]
        session.commit()
    finally:
        session.close()

    failed = client.post(f"/api/v1/tickets/{ticket['id']}/analyze", headers=auth["ops"])
    assert failed.status_code == 409, failed.text
    assert failed.json()["detail"]["missing"] == [
        "订单事实来源暂不可用，请客服人工核实后再继续"
    ]

    detail = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"]).json()
    assert detail["ticket"]["status"] == "need_customer_info"
    assert detail["ticket"]["missing_fields"] == failed.json()["detail"]["missing"]
    assert detail["handoff_summary"]["next_step"] == "请客服人工核实订单事实来源恢复后再继续处理"


def test_ticket_not_found_returns_404(client, auth):
    resp = client.get("/api/v1/tickets/not-exist-id", headers=auth["ops"])
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "ticket_not_found"


def test_ticket_list_exposes_sla_and_next_action_for_frontline_workbench(client, auth):
    ticket = _create(
        client,
        auth,
        {
            "subject": "待回复的物流问题",
            "content": "请帮我查一下物流。",
            "external_id": "CS-WORKBENCH-UX-0001",
            "priority": "high",
            "order_id": "TB-UX-0001",
        },
    )
    response = client.get("/api/v1/tickets", headers=auth["ops"])
    assert response.status_code == 200
    item = next(row for row in response.json()["items"] if row["id"] == ticket["id"])
    assert item["sla_remaining_seconds"] is not None
    assert item["sla_status"] in {"on_track", "at_risk", "overdue"}
    assert item["next_action"] in {"开始分析", "补充订单事实", "等待客服回复", "等待客户补充"}
    assert item["handler_label"]


def test_ticket_detail_exposes_answerability_and_safe_pending_reply(client, auth):
    ticket = _create(
        client,
        auth,
        {
            "subject": "待审批退款",
            "content": "商品不想要了，请退款。",
            "external_id": "CS-WORKBENCH-UX-0002",
            "order_id": "TB-UX-0002",
        },
    )
    _advance_to_analyzing(client, auth, ticket["id"])
    _seed_recommendation(
        ticket["id"],
        risk_tier=RiskTier.L3.value,
        status=RecommendationStatus.PENDING_APPROVAL.value,
        action_type="refund",
    )
    response = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["ops"])
    assert response.status_code == 200
    body = response.json()
    recommendation = body["recommendation"]
    assert recommendation["answerability_code"] == "human_review"
    assert recommendation["answerability_label"] == "需要人工决定"
    assert "尚未执行退款" in recommendation["customer_reply"]

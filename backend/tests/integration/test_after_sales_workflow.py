"""售后工单工作流集成测试（真实库 + stub 模型 + 同步执行）。

这是"7 个 Agent 拓扑真的跑起来了吗"的验收：从**接口**触发一次分析，
然后检查落库结果（建议 / 引用 / 风险 / 工单状态 / Agent 轨迹）。

需求书的几条硬要求在断言里：
- §3.2 Agent 输出必须是结构化 Schema（检查 `agent_runs` 有 7 个 Agent 的记录）；
- §3.3 风险分级与审批要求必须落在建议上，L3 必须进审批；
- §1.2 金额只能引用事实（退款 = 实付金额），且未批准不允许发送；
- §2.3 工单状态由后端推进（低风险 → 待回复；高风险 → 待审批）。
"""

from __future__ import annotations

import pytest
import time

from app.db.session import SessionLocal

CREATED_TICKETS: list[str] = []


@pytest.fixture(autouse=True, scope="module")
def purge_created_data():
    """测试结束清理自己造的数据。

    为什么必须清理：集成测试共用同一个库，而灰度实验接口统计的是**全租户**的任务与结论，
    本模块的分析任务会把它的 `avg_findings` 拉低，导致另一条测试随执行顺序偶发失败
    （实测踩过：单独跑过、一起跑挂）。修根因是"我的测试不留垃圾"，而不是削弱那条断言。
    """
    CREATED_TICKETS.clear()
    yield
    if not CREATED_TICKETS:
        return
    session = SessionLocal()
    try:
        from app.models.ticket import OrderFact, Recommendation, Ticket, TicketMessage
        from app.models.workflow import (
            AgentRun,
            AnalysisEvent,
            AnalysisTask,
            Approval,
            EvidenceCitation,
            Report,
            ToolCall,
        )

        task_ids: list[str] = []
        for ticket_id in CREATED_TICKETS:
            ticket = session.get(Ticket, ticket_id)
            if ticket and ticket.last_analysis_task_id:
                task_ids.append(ticket.last_analysis_task_id)
            # 同一工单可能跑过多次分析：把所有以该工单为 filters.ticket_id 的任务都清掉
            for row in session.query(AnalysisTask).all():
                if (row.filters or {}).get("ticket_id") == ticket_id:
                    task_ids.append(row.id)

        for task_id in set(task_ids):
            for model in (AgentRun, AnalysisEvent, ToolCall, EvidenceCitation, Approval, Report):
                session.query(model).filter(model.task_id == task_id).delete()
            row = session.get(AnalysisTask, task_id)
            if row is not None:
                session.delete(row)
        for ticket_id in CREATED_TICKETS:
            for model in (OrderFact, TicketMessage, Recommendation):
                session.query(model).filter(model.ticket_id == ticket_id).delete()
            ticket = session.get(Ticket, ticket_id)
            if ticket is not None:
                session.delete(ticket)
        session.commit()
    finally:
        session.close()


def _create(client, auth, payload: dict) -> dict:
    resp = client.post("/api/v1/tickets", json=payload, headers=auth["ops"])
    assert resp.status_code == 201, resp.text
    created = resp.json()
    if created["id"] not in CREATED_TICKETS:
        CREATED_TICKETS.append(created["id"])
    return created


def _analyze(client, auth, ticket_id: str):
    response = client.post(f"/api/v1/tickets/{ticket_id}/analyze", headers=auth["ops"])
    if response.status_code != 202:
        return response

    run_id = response.json()["run_id"]
    from app.models.workflow import AnalysisTask

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        session = SessionLocal()
        try:
            task = session.get(AnalysisTask, run_id)
            assert task is not None, f"分析任务不存在: {run_id}"
            if task.status in {"completed", "failed", "cancelled", "waiting_human"}:
                assert task.status != "failed", f"售后工作流失败: {task.error}"
                return response
        finally:
            session.close()
        time.sleep(0.02)
    pytest.fail(f"售后工作流未在 15 秒内结束: {run_id}")


def _detail(client, auth, ticket_id: str) -> dict:
    resp = client.get(f"/api/v1/tickets/{ticket_id}", headers=auth["ops"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def _agent_codes(ticket_id: str) -> list[str]:
    """查这次分析里实际执行过的 Agent。"""
    session = SessionLocal()
    try:
        from app.models.ticket import Ticket
        from app.models.workflow import AgentRun

        ticket = session.get(Ticket, ticket_id)
        runs = (
            session.query(AgentRun)
            .filter(AgentRun.task_id == ticket.last_analysis_task_id)
            .all()
        )
        return sorted({row.agent_code for row in runs})
    finally:
        session.close()


def test_low_risk_ticket_gets_draft_client(client, auth):
    """L1 低风险（优惠口径解释）：自动出草稿，工单进入待回复，不需要审批。"""
    ticket = _create(
        client,
        auth,
        {
            "subject": "满减没有生效",
            "content": "活动页显示满 199 减 30，我结算时没有减，请帮我看看优惠规则。",
            "external_id": "WF-TEST-0001",
            "order_id": "TB202609240821",
        },
    )
    resp = _analyze(client, auth, ticket["id"])
    assert resp.status_code == 202, resp.text

    detail = _detail(client, auth, ticket["id"])
    recommendation = detail["recommendation"]
    assert recommendation is not None, "分析必须产出处理建议"
    policy_refs = recommendation.get("policy_refs") or []
    promotion_terms = ("满减", "促销活动", "优惠活动", "活动优惠", "优惠券规则", "优惠券使用", "折扣规则", "活动门槛", "优惠规则")
    assert all(
        any(term in f"{ref.get('title', '')} {ref.get('snippet', '')}" for term in promotion_terms)
        for ref in policy_refs
    ), "优惠问题不能引用物流等其他场景的政策"
    if not policy_refs:
        assert recommendation["risk_tier"] == "L2"
        assert recommendation["approval_requirement"] == "staff_confirm"
        assert recommendation["answerability_code"] == "staff_confirm"
        assert recommendation["answerability_label"] == "客服核对后回复"
    assert recommendation["action_type"] in ("reply_only", "contact_logistics", "ask_customer")
    assert recommendation["risk_tier"] in ("L1", "L2")
    assert recommendation["customer_reply"], "低风险必须直接给出回复草稿"
    assert "冻干零食" in recommendation["customer_reply"]
    assert "179.00 元" in recommendation["customer_reply"]
    assert "满 199 减 30" in recommendation["customer_reply"]
    assert "未达到活动门槛" in recommendation["customer_reply"]
    assert "正在核实相关规则" not in recommendation["customer_reply"]
    assert detail["ticket"]["status"] in ("ready_to_reply", "waiting_review")

    analysis = detail["analysis"]
    assert analysis["mode"] == "fast_path"
    assert analysis["mode_label"] == "快速核验"
    assert analysis["applied_skills"] == ["promotion_explanation"]
    assert analysis["agent_count"] == 4
    assert len(analysis["business_steps"]) == 4
    assert {step["agent_code"] for step in analysis["business_steps"]} == {
        "triage_agent",
        "order_fact_agent",
        "policy_rag_agent",
        "review_agent",
    }
    assert all(step["role"] and step["responsibility"] and step["conclusion"] for step in analysis["business_steps"])
    assert analysis["steps"] == []
    assert analysis["analysis_task_id"] is None
    assert detail["cost"] is None
    assert not {"model", "total_tokens", "latency_ms", "error"}.intersection(
        key for step in analysis["business_steps"] for key in step
    )

    codes = _agent_codes(ticket["id"])
    assert set(codes) == {
        "triage_agent",
        "order_fact_agent",
        "policy_rag_agent",
        "review_agent",
    }, f"低风险快路径不应启动方案润色、回复生成和 Agent 跟进：{codes}"

    session = SessionLocal()
    try:
        from app.models.workflow import AgentRun, AnalysisTask

        task = session.get(AnalysisTask, detail["ticket"]["last_analysis_task_id"])
        assert task is not None
        assert task.filters["multi_agent"] is False
        assert task.filters["business_skills"] == [
            {"code": "promotion_explanation", "version": "1.0.0"}
        ]
        runs = session.query(AgentRun).filter(AgentRun.task_id == task.id).all()
        assert runs
        assert all((run.output or {}).get("model_used") is False for run in runs)
        assert all(
            (run.output or {}).get("business_skills") == [
                {"code": "promotion_explanation", "version": "1.0.0"}
            ]
            for run in runs
        )
    finally:
        session.close()


def test_fact_adapter_timeout_stops_analysis_without_fabricated_recommendation(client, auth):
    ticket = _create(
        client,
        auth,
        {
            "subject": "物流事实接口超时",
            "content": "物流一直没有更新，请帮我核实。",
            "external_id": "WF-TEST-FACT-TIMEOUT",
            "order_id": "TB20260101TIMEOUT",
        },
    )

    response = _analyze(client, auth, ticket["id"])

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "need_customer_info"
    assert detail["missing"] == ["订单事实来源暂不可用，请客服人工核实后再继续"]
    ticket_detail = _detail(client, auth, ticket["id"])
    assert ticket_detail["ticket"]["status"] == "need_customer_info"
    assert ticket_detail["ticket"]["missing_fields"] == detail["missing"]
    assert ticket_detail["recommendation"] is None
    assert ticket_detail["ticket"]["last_analysis_task_id"] is None
    assert ticket_detail["handoff_summary"]["next_step"] == "请客服人工核实订单事实来源恢复后再继续处理"


def test_refund_ticket_requires_supervisor_approval(client, auth):
    """退款 → L3 → 必须等待主管审批；金额只能引用实付金额。"""
    ticket = _create(
        client,
        auth,
        {
            "subject": "申请退款",
            "content": "商品不想要了，申请退款，请把钱退给我。",
            "external_id": "WF-TEST-0002",
            "order_id": "TB202609241501",
        },
    )
    resp = _analyze(client, auth, ticket["id"])
    assert resp.status_code == 202, resp.text

    detail = _detail(client, auth, ticket["id"])
    recommendation = detail["recommendation"]
    assert recommendation["action_type"] == "refund"
    assert recommendation["risk_tier"] == "L3"
    assert recommendation["approval_requirement"] == "supervisor"
    assert recommendation["risk_reason"], "风险原因必须是人话（供审批人阅读）"
    # 金额来自支付事实（TB202609241501 实付 89.00），不是模型算出来的
    assert str(recommendation["amount"]).startswith("89")
    assert "支付事实" in (recommendation["rationale"] or "") or "实付" in (
        recommendation["rationale"] or ""
    )
    assert recommendation["status"] == "pending_approval"
    assert detail["ticket"]["status"] == "waiting_review"
    # 金额来源必须落库并回给界面：审批人要能核对"这 89 元是订单实付"而不是模型算的
    assert "支付事实" in (recommendation.get("amount_source") or ""), recommendation.get(
        "amount_source"
    )
    refund_policy_terms = ("退货退款", "退款与审批", "退款申请", "退款金额", "退款条件", "退款审核", "退款规则", "退款政策", "退款标准")
    assert all(
        any(term in f"{ref.get('title', '')} {ref.get('clause', '')} {ref.get('snippet', '')}" for term in refund_policy_terms)
        for ref in (recommendation.get("policy_refs") or [])
    ), "退款建议不能引用只顺带提及退款的物流补偿条款"
    analysis = detail["analysis"]
    assert analysis["mode"] == "multi_agent"
    assert analysis["mode_label"] == "多 Agent 协作"
    assert analysis["applied_skills"] == ["refund_review", "quality_guard"]
    assert analysis["agent_count"] == 7
    assert len(analysis["business_steps"]) == 7
    assert "退款" in analysis["route_reason"]
    steps = {step["agent_code"]: step for step in analysis["business_steps"]}
    assert steps["order_fact_agent"]["parallel_group"] == "订单与政策核验"
    assert steps["policy_rag_agent"]["parallel_group"] == "订单与政策核验"
    assert steps["response_agent"]["parallel_group"] == "回复准备与风险复核"
    assert steps["review_agent"]["parallel_group"] == "回复准备与风险复核"
    assert analysis["steps"] == []
    assert detail["cost"] is None

    public_events = client.get(f"/api/v1/runs/{detail['ticket']['last_analysis_task_id']}/events", headers=auth["ops"])
    assert public_events.status_code == 200, public_events.text
    assert "正在核对订单、支付和物流记录" in public_events.text
    assert "triage_agent" not in public_events.text
    assert "payload" not in public_events.text

    technical_events = client.get(
        f"/api/v1/runs/{detail['ticket']['last_analysis_task_id']}/events", headers=auth["eng"]
    )
    assert technical_events.status_code == 200, technical_events.text
    assert '"node": "triage_agent"' in technical_events.text

    engineering_view = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["eng"])
    assert engineering_view.status_code == 200, engineering_view.text
    technical_steps = engineering_view.json()["analysis"]["steps"]
    assert len(technical_steps) == 7
    assert all("model" in step and "total_tokens" in step for step in technical_steps)
    assert engineering_view.json()["cost"] is not None
    assert client.get(
        f"/api/v1/tickets/{ticket['id']}/cost", headers=auth["ops"]
    ).status_code == 403
    engineering_cost = client.get(f"/api/v1/tickets/{ticket['id']}/cost", headers=auth["eng"])
    assert engineering_cost.status_code == 200, engineering_cost.text
    assert "total_tokens" in engineering_cost.json()


def test_reflection_enabled_revises_refund_reply_before_persisting(client, auth, monkeypatch):
    """退款草稿先复核、再修订、再次复核，最终只落安全版本。"""
    from app.config import get_settings
    from app.db.session import SessionLocal
    from app.models.workflow import AnalysisEvent, AnalysisTask
    from app.services import ticket_analysis, workflow_runner

    enabled_settings = get_settings().model_copy(
        update={"reflection_enabled": True, "max_reflection_rounds": 1}
    )
    monkeypatch.setattr(ticket_analysis, "get_settings", lambda: enabled_settings)
    workflow_runner.reset_runner()
    workflow_runner._runner = workflow_runner.WorkflowRunner(
        settings=enabled_settings
    )
    try:
        ticket = _create(
            client,
            auth,
            {
                "subject": "退款回复反思",
                "content": "商品不想要了，申请退款，请把钱退给我。",
                "external_id": "WF-TEST-REFLECTION-0001",
                "order_id": "TB202609241501",
            },
        )
        response = _analyze(client, auth, ticket["id"])
        assert response.status_code == 202, response.text

        detail = _detail(client, auth, ticket["id"])
        recommendation = detail["recommendation"]
        assert recommendation["status"] == "pending_approval"
        assert "审批通过后" in recommendation["customer_reply"]
        assert "将为您发起退款" not in recommendation["customer_reply"]
        assert "预计 1-3 个工作日到账" not in recommendation["customer_reply"]

        session = SessionLocal()
        try:
            task = session.get(AnalysisTask, detail["ticket"]["last_analysis_task_id"])
            assert task is not None and task.status == "completed"
            events = (
                session.query(AnalysisEvent)
                .filter(AnalysisEvent.task_id == task.id)
                .order_by(AnalysisEvent.created_at.asc())
                .all()
            )
            messages = [item.message for item in events]
            assert "正在复核客户回复" in messages
            assert any("已完成一次回复修订" in item for item in messages)
            assert sum("回复质量复核已完成" in item or "回复复核发现" in item for item in messages) == 2
        finally:
            session.close()
    finally:
        workflow_runner.reset_runner()


def test_ticket_message_idempotency_prevents_replay_duplicates(client, auth):
    from app.db.session import SessionLocal
    from app.models.ticket import TicketMessage
    from app.services.ticket_service import TicketService

    ticket = _create(
        client,
        auth,
        {
            "subject": "消息幂等验证",
            "content": "请帮我核实订单。",
            "external_id": "WF-TEST-MESSAGE-IDEMPOTENCY",
            "order_id": "TB202609240821",
        },
    )
    session = SessionLocal()
    try:
        service = TicketService(session)
        row = service.get(ticket["id"])
        first = service.add_message(
            row,
            content="同一草稿只应写入一次",
            sender_type="agent",
            visible_to_customer=False,
            idempotency_key="draft:task-1:v1",
        )
        second = service.add_message(
            row,
            content="同一草稿只应写入一次",
            sender_type="agent",
            visible_to_customer=False,
            idempotency_key="draft:task-1:v1",
        )
        assert first.id == second.id
        assert (
            session.query(TicketMessage)
            .filter(TicketMessage.ticket_id == row.id)
            .filter(TicketMessage.idempotency_key == "draft:task-1:v1")
            .count()
            == 1
        )
    finally:
        session.close()


def test_stalled_logistics_uses_policy_and_followup(client, auth):
    """物流停滞 56 小时：动作=联系物流，进入跟进计划；政策未命中时标注缺口。"""
    ticket = _create(
        client,
        auth,
        {
            "subject": "物流停滞",
            "content": "包裹物流超过 48 小时没有更新了，麻烦帮我催一下。",
            "external_id": "WF-TEST-0003",
            "order_id": "TB202609240655",
        },
    )
    assert _analyze(client, auth, ticket["id"]).status_code == 202

    detail = _detail(client, auth, ticket["id"])
    recommendation = detail["recommendation"]
    assert recommendation["action_type"] == "contact_logistics"
    # 补偿金额来自**政策条款**（不是订单实付），来源必须落库，否则审批人无法核对
    assert "政策条款" in (recommendation.get("amount_source") or ""), recommendation.get(
        "amount_source"
    )
    # 停产 56 小时 → 需要跟进
    followup = (detail["ticket"].get("attributes") or {}).get("followup")
    assert followup and followup["required"] is True
    assert followup["next_check_hours"] >= 1
    assert followup["reminder"]


def test_seven_agents_are_traced(client, auth):
    """高风险工单必须保留 §3.2 的完整 7 Agent 轨迹。"""
    ticket = _create(
        client,
        auth,
        {
            "subject": "申请退款",
            "content": "商品不想要了，申请退款，请把钱退给我。",
            "external_id": "WF-TEST-0004",
            "order_id": "TB202609241501",
        },
    )
    assert _analyze(client, auth, ticket["id"]).status_code == 202
    codes = _agent_codes(ticket["id"])
    expected = {
        "triage_agent",
        "order_fact_agent",
        "policy_rag_agent",
        "resolution_agent",
        "response_agent",
        "review_agent",
        "followup_agent",
    }
    assert expected <= set(codes), f"缺少 Agent 轨迹: {expected - set(codes)}"


def test_low_initial_risk_refund_is_promoted_to_full_agent_flow(client, auth):
    """创建时的 L1 标签不能盖过正文里的退款请求与核验事实风险。"""
    ticket = _create(
        client,
        auth,
        {
            "subject": "退款请求风险复核",
            "content": "商品不想要了，申请退款，请把钱退给我。",
            "external_id": "WF-TEST-0009",
            "order_id": "TB202609241501",
        },
    )
    assert _analyze(client, auth, ticket["id"]).status_code == 202

    detail = _detail(client, auth, ticket["id"])
    assert detail["recommendation"]["risk_tier"] == "L3"
    assert detail["recommendation"]["status"] == "pending_approval"
    assert set(_agent_codes(ticket["id"])) >= {
        "triage_agent",
        "order_fact_agent",
        "policy_rag_agent",
        "resolution_agent",
        "response_agent",
        "review_agent",
        "followup_agent",
    }


def test_analysis_records_facts_and_timeline(client, auth):
    """事实、消息与时间线都要落库（详情页的数据来源）。"""
    ticket = _create(
        client,
        auth,
        {
            "subject": "破损",
            "content": "收到商品外包装破损，担心影响使用。",
            "external_id": "WF-TEST-0005",
            "order_id": "TB202609240992",
        },
    )
    assert _analyze(client, auth, ticket["id"]).status_code == 202
    detail = _detail(client, auth, ticket["id"])
    assert len(detail["facts"]) >= 4
    # AI 草稿入消息流（sender_type=agent，且对客户不可见）
    assert any(m["sender_type"] == "agent" for m in detail["messages"])
    kinds = [item["kind"] for item in detail["timeline"]]
    assert "created" in kinds
    assert detail["analysis"]["business_steps"], "客服应能查看业务化协作流程"
    assert detail["analysis"]["steps"] == [], "客服不应从接口读取技术轨迹"
    technical = client.get(f"/api/v1/tickets/{ticket['id']}", headers=auth["eng"])
    assert technical.status_code == 200, technical.text
    assert technical.json()["analysis"]["steps"], "工程人员仍应能查看技术轨迹"


def test_controlled_fields_come_from_rules_not_model(client, auth):
    """受控字段（动作/风险/问题类型）必须来自规则，不能被模型的润色改写。

    回归背景（真实模型下实测）：模型把 `action_type` 从 refund 润色成 ask_customer、
    把 `issue_type` 写成中文标签，导致风险等级从 L3 掉到 L2 —— 本该主管审批的退款
    变成可自动发送的草稿。现在这三项由 `decide_action()` 规则决定，模型只能润色文字。
    """
    ticket = _create(
        client,
        auth,
        {
            "subject": "受控字段",
            "content": "商品不想要了，申请退款，请把钱退给我。",
            "external_id": "WF-TEST-0007",
            "order_id": "TB202609241501",
        },
    )
    assert _analyze(client, auth, ticket["id"]).status_code == 202
    detail = _detail(client, auth, ticket["id"])

    # 问题类型必须是枚举值（中文标签会把统计口径污染掉）
    assert detail["ticket"]["issue_type"] == "refund"
    recommendation = detail["recommendation"]
    assert recommendation["action_type"] == "refund"
    assert recommendation["risk_tier"] == "L3"
    assert recommendation["approval_requirement"] == "supervisor"
    assert recommendation["status"] == "pending_approval"
    assert detail["ticket"]["status"] == "waiting_review"
    # 金额引用实付金额（89.00），不是模型算出来的
    assert str(recommendation["amount"]).startswith("89")


def test_analyze_is_idempotent_while_running(client, auth):
    """同一工单重复点分析不应叠加运行次数（成本保护）。"""
    ticket = _create(
        client,
        auth,
        {
            "subject": "重复分析",
            "content": "物流停滞两天了，帮我看下能不能补偿。",
            "external_id": "WF-TEST-0006",
            "order_id": "TB202609240655",
        },
    )
    first = _analyze(client, auth, ticket["id"])
    assert first.status_code == 202
    run_id = first.json()["run_id"]
    # 同步执行完后工单已不在 analyzing；再次触发会新建一轮（这是允许的），
    # 但**同一时刻**的重复请求必须复用同一 run —— 用服务层直接验证该语义
    from app.db.session import SessionLocal as _Session
    from app.models.identity import User
    from app.models.workflow import AnalysisTask
    from app.services.ticket_analysis import start_analysis
    from app.services.ticket_service import TicketService

    session = _Session()
    try:
        service = TicketService(session)
        row = service.get(ticket["id"])
        row.status = "analyzing"
        row.last_analysis_task_id = run_id
        # "运行还在跑"才叫并发重复点击：把任务置回 running
        task = session.get(AnalysisTask, run_id)
        task.status = "running"
        session.commit()

        user = session.query(User).filter(User.username == "ops").first()
        assert start_analysis(session, ticket=row, user=user) == run_id
    finally:
        session.close()


def test_ticket_analysis_persists_reflection_configuration_for_business_view(
    client, auth, monkeypatch
):
    """详情页必须读取任务创建时的反思配置，不能只依赖图内临时 state。"""
    from app.config import get_settings
    from app.db.session import SessionLocal as _Session
    from app.models.workflow import AnalysisTask
    from app.services import ticket_analysis

    enabled = get_settings().model_copy(
        update={
            "reflection_enabled": True,
            "reflection_ticket_enabled": True,
            "max_reflection_rounds": 2,
        }
    )
    monkeypatch.setattr(ticket_analysis, "get_settings", lambda: enabled)

    ticket = _create(
        client,
        auth,
        {
            "subject": "反思配置持久化",
            "content": "物流显示已签收，但我没有收到包裹，请帮忙核实。",
            "external_id": "WF-REFLECTION-CONFIG-0001",
            "order_id": "TB202609241806",
        },
    )
    response = _analyze(client, auth, ticket["id"])
    assert response.status_code == 202

    session = _Session()
    try:
        task = session.get(AnalysisTask, response.json()["run_id"])
        assert task.filters["reflection_enabled"] is True
        assert task.filters["max_reflection_rounds"] == 2
    finally:
        session.close()


def test_failed_run_does_not_strand_ticket_in_analyzing(client, auth):
    """回归：分析运行失败后，工单不能永久卡在"分析中"。

    踩过的坑（演示脚本一次暴露 6 张）：运行器的 `_fail` 只改 `AnalysisTask.status`，
    工单留在 `analyzing`；而 `start_analysis` 又"分析中不重复起"，
    于是前端的「重新分析」变成哑按钮 —— 失败既看不见也点不动。
    现在两处都补上：失败回写工单状态（可重试），且已结束的运行不再阻止重新分析。
    """
    from app.db.session import SessionLocal as _Session
    from app.models.identity import User
    from app.models.workflow import AnalysisTask
    from app.services.ticket_analysis import start_analysis
    from app.services.ticket_service import TicketService

    ticket = _create(
        client,
        auth,
        {
            "subject": "失败重试",
            "content": "包裹破损，需要补发。",
            "external_id": "WF-TEST-0008",
            "order_id": "TB202609240992",
        },
    )
    assert _analyze(client, auth, ticket["id"]).status_code == 202

    session = _Session()
    try:
        service = TicketService(session)
        row = service.get(ticket["id"])
        old_run = row.last_analysis_task_id
        # 伪造"这次运行失败了且工单卡在分析中"
        task = session.get(AnalysisTask, old_run)
        task.status = "failed"
        task.error = {"code": "RuntimeError", "message": "模拟失败"}
        row.status = "analyzing"
        session.commit()

        # 1) 失败要回写工单状态（可见）
        service.sync_analysis_aborted(ticket_id=row.id, run_id=old_run, error="模拟失败")
        session.expire_all()
        assert service.get(ticket["id"]).status == "failed"

        # 2) 失败后重新分析必须真的新建一轮，而不是复用那个死掉的 run
        row = service.get(ticket["id"])
        user = session.query(User).filter(User.username == "ops").first()
        new_run = start_analysis(session, ticket=row, user=user)
        assert new_run != old_run, "失败后重新分析必须新建运行"
        session.expire_all()
        assert service.latest_recommendation(ticket["id"]) is not None
        assert service.get(ticket["id"]).status in ("ready_to_reply", "waiting_review")
    finally:
        session.close()

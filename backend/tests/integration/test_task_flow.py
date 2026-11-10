"""集成测试：完整诊断链路（创建 → 执行 → 人工审核 → 报告 → 引用）。"""

from __future__ import annotations

QUESTION = (
    "Android 端 3.2.0 发布后次日留存下降，哪些用户受到影响？"
    "版本变更、用户反馈和故障记录是否支持版本相关性？下一步应该优先验证什么？"
)
FILTERS = {
    "product": "星野社区",
    "platform": "Android",
    "app_version": "3.2.0",
    "date_from": "2026-08-01",
    "date_to": "2026-08-31",
    "knowledge_bases": ["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
    "metrics": ["next_day_retention", "crash_rate"],
}


def _create_task(client, auth, payload: dict) -> dict:
    resp = client.post("/api/v1/tasks", headers=auth["pm"], json=payload)
    assert resp.status_code == 202, resp.text
    return resp.json()


def test_full_diagnosis_flow_with_human_approval(client, auth):
    """核心场景：证据冲突 → 人工审核 → 报告落地并带可定位引用。"""
    task = _create_task(client, auth, {"query": QUESTION, **FILTERS, "priority": "P1"})
    assert task["status"] in {"waiting_human", "completed"}

    if task["status"] == "waiting_human":
        pending = client.get("/api/v1/approvals", headers=auth["reviewer"])
        assert pending.status_code == 200
        assert any(item["task_id"] == task["id"] for item in pending.json()["items"])

        approved = client.post(
            f"/api/v1/tasks/{task['id']}/approve",
            headers=auth["reviewer"],
            json={"action": "approve", "comment": "证据充分，同意出报告"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "completed"

    detail = client.get(f"/api/v1/tasks/{task['id']}", headers=auth["pm"]).json()
    assert detail["status"] == "completed"
    assert detail["intent"] == "version_impact"
    assert detail["plan"], "执行计划必须落库"

    report_resp = client.get(f"/api/v1/tasks/{task['id']}/report", headers=auth["pm"])
    assert report_resp.status_code == 200, report_resp.text
    report = report_resp.json()

    # 报告必须区分事实/推断/假设/建议，并给出优先级与置信度
    assert report["facts"], "报告应包含事实"
    assert report["priority"] in {"P0", "P1", "P2", "P3"}
    assert 0.0 <= report["confidence"] <= 1.0
    assert report["summary"]

    # 关键结论必须可定位：每条事实都要有引用记录
    cited_statements = {item["statement_id"] for item in report["citations"]}
    for index, fact in enumerate(report["facts"], start=1):
        assert fact.get("evidence_refs"), f"事实 {fact.get('id')} 缺少证据引用"
        statement_id = fact.get("id") or f"facts-{index}"
        assert statement_id in cited_statements, f"事实 {statement_id} 没有引用记录"
    assert all(item["permission_checked"] for item in report["citations"])

    # 指标证据要带数据版本；文档证据要能定位到来源
    metric_citations = [
        item for item in report["citations"] if item["source_kind"] == "metric"
    ]
    doc_citations = [item for item in report["citations"] if item["source_kind"] == "document"]
    assert metric_citations, "应至少有一条指标引用"
    assert all(item["metric_ref"]["dataset_version"] for item in metric_citations)
    assert doc_citations and all(item["source_name"] for item in doc_citations)

    # 导出
    export = client.get(f"/api/v1/reports/{report['id']}/export?format=md", headers=auth["pm"])
    assert export.status_code == 200
    assert "产品问题诊断报告" in export.text
    assert "## 事实" in export.text


def test_conflict_is_preserved_not_merged(client, auth):
    """冲突证据不得被强行合并：要么出现在报告里，要么任务被挂起等人工。"""
    task = _create_task(client, auth, {"query": QUESTION, **FILTERS})
    detail = client.get(f"/api/v1/tasks/{task['id']}", headers=auth["pm"]).json()

    if detail["status"] == "waiting_human":
        approvals = client.get("/api/v1/approvals", headers=auth["reviewer"]).json()
        current = [item for item in approvals["items"] if item["task_id"] == task["id"]]
        assert current, "等待人工审核时必须存在待审核记录"
        request = current[0]["request"]
        assert request["conflicts"], "审核请求里必须带上冲突双方的结论"
        assert request["coverage"]["evidence_count"] > 0
        client.post(
            f"/api/v1/tasks/{task['id']}/approve",
            headers=auth["reviewer"],
            json={"action": "approve", "comment": "保留冲突，按来源优先级出结论"},
        )

    report = client.get(f"/api/v1/tasks/{task['id']}/report", headers=auth["pm"]).json()
    if report["conflicts"]:
        for conflict in report["conflicts"]:
            assert conflict["statement_a"]["text"] and conflict["statement_b"]["text"]
            resolution = conflict.get("resolution")
            if resolution:
                assert resolution["priority_rule"], "冲突裁决必须说明来源优先级规则"
                assert resolution["adopted_statement"]


def test_trace_and_events_record_agent_and_tool_activity(client, auth):
    task = _create_task(client, auth, {"query": QUESTION, **FILTERS})
    trace = client.get(f"/api/v1/tasks/{task['id']}/trace", headers=auth["pm"]).json()
    codes = {run["agent_code"] for run in trace["agent_runs"]}
    assert {"product_agent", "feedback_agent", "release_agent", "metric_agent"} <= codes
    assert trace["tool_calls"], "工具调用必须留痕"
    assert all(call["policy_decision"] for call in trace["tool_calls"])
    assert trace["totals"]["denied_tool_calls"] == 0

    events = client.get(f"/api/v1/tasks/{task['id']}/events", headers=auth["pm"]).json()
    nodes = {item["node"] for item in events["items"]}
    assert {"validate_request", "classify_intent", "create_plan", "merge_evidence"} <= nodes


def test_idempotency_key_prevents_duplicate_tasks(client, auth):
    payload = {"query": QUESTION, **FILTERS, "idempotency_key": "itest-idem-001"}
    first = _create_task(client, auth, payload)
    second = _create_task(client, auth, payload)
    assert first["id"] == second["id"], "相同幂等键必须复用同一任务"


def test_report_reflection_revises_unreferenced_fact_before_persisting(
    client, auth, monkeypatch
):
    """报告事实缺少引用时生成 v2，将其降级为待验证假设后再落库。"""
    from app.agents.report_agent import ReportAgent
    from app.config import get_settings
    from app.services import workflow_runner

    def unreferenced_report(self, ai, collected):
        return {
            "summary": "测试草稿包含一条没有引用的事实。",
            "findings": [
                {
                    "text": "这是一条缺少证据引用的事实",
                    "type": "fact",
                    "evidence_refs": [],
                    "confidence": 0.8,
                }
            ],
            "impact_scope": [],
            "hypotheses": [],
            "recommendations": [],
            "priority": "P1",
            "confidence": 0.7,
        }

    monkeypatch.setattr(ReportAgent, "deterministic_output", unreferenced_report)
    workflow_runner.reset_runner()
    workflow_runner._runner = workflow_runner.WorkflowRunner(
        settings=get_settings().model_copy(
            update={"reflection_enabled": True, "max_reflection_rounds": 1}
        )
    )
    try:
        task = _create_task(client, auth, {"query": QUESTION, **FILTERS})
        if task["status"] == "waiting_human":
            approved = client.post(
                f"/api/v1/tasks/{task['id']}/approve",
                headers=auth["reviewer"],
                json={"action": "approve", "comment": "允许继续生成测试报告"},
            )
            assert approved.status_code == 200, approved.text

        detail = client.get(f"/api/v1/tasks/{task['id']}", headers=auth["pm"]).json()
        assert detail["status"] == "completed", detail
        report = client.get(f"/api/v1/tasks/{task['id']}/report", headers=auth["pm"]).json()
        assert report["facts"] == []
        assert any("缺少证据引用" in item["text"] for item in report["hypotheses"])

        events = client.get(f"/api/v1/tasks/{task['id']}/events", headers=auth["pm"]).json()
        messages = [item["message"] for item in events["items"]]
        assert any("报告复核发现" in item for item in messages)
        assert any("已完成一次报告修订" in item for item in messages)
        assert any("报告质量复核已完成" in item for item in messages)
    finally:
        workflow_runner.reset_runner()

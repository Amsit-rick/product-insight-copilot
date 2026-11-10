"""售后工单评测接口的集成测试（需求书 §9）。

只跑很小的规模（limit=6）：评测接口会真实执行工作流，测试里跑全量 228 条没有必要，
**但要验证接口契约与指标结构**——尤其三个安全指标必须存在（审批门禁召回、
失效政策泄漏率、知识库隔离率），它们是这套评测存在的意义。
"""

from __future__ import annotations


def test_ticket_eval_cases_endpoint_lists_dataset(client, auth):
    resp = client.get("/api/v1/evaluations/tickets/cases", params={"limit": 5}, headers=auth["ops"])
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["dataset_version"] == "tickets-1.1.0"
    assert payload["total"] == 5
    first = payload["items"][0]
    assert {"case_id", "group", "subject", "expect"} <= set(first)
    assert first["expect"], "用例必须带期望，否则无法判定"


def test_ticket_eval_cases_endpoint_estimates_real_model_cost(client, auth, monkeypatch):
    """真实模型模式也必须能读取用例列表，不能因缺少数据库会话而 500。"""
    from sqlalchemy.orm import Session

    from app.api import routes_evaluations

    monkeypatch.setattr(routes_evaluations, "real_model_enabled", lambda: True)

    def fake_estimate(session: Session) -> float:
        assert isinstance(session, Session)
        return 0.12

    monkeypatch.setattr(routes_evaluations, "estimated_ticket_case_cost", fake_estimate)
    resp = client.get("/api/v1/evaluations/tickets/cases", params={"limit": 5}, headers=auth["ops"])

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["real_model"] is True
    assert payload["estimated_cost_per_case"] == 0.12
    assert payload["estimated_cost_total"] == 0.6


def test_ticket_eval_requires_permission(client, auth):
    """客服（ops）没有 eval:run 权限，不能触发评测。"""
    resp = client.post("/api/v1/evaluations/tickets/run", params={"limit": 1}, headers=auth["ops"])
    assert resp.status_code == 403


def test_metrics_guide_includes_ticket_metrics(client, auth):
    """指标说明必须覆盖工单指标：界面上不能靠猜字段含义（此前这一项是缺的）。"""
    resp = client.get("/api/v1/evaluations/metrics-guide", headers=auth["ops"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ticket_metrics = body.get("ticket_metrics") or {}
    assert ticket_metrics, "缺少工单指标说明"
    for key in (
        "pass_rate",
        "issue_macro_f1",
        "approval_gate_recall",
        "expired_policy_leak_rate",
        "tenant_isolation_rate",
        "avg_cost_amount",
    ):
        assert key in ticket_metrics, f"工单指标说明缺少 {key}"
    # 三个安全指标必须在说明里标明是安全项（否则会被当成普通通过率看待）
    assert "【安全】" in ticket_metrics["approval_gate_recall"]
    assert "【安全】" in ticket_metrics["expired_policy_leak_rate"]
    assert body.get("ticket_groups"), "缺少工单评测分组清单"

    # 分组清单必须与数据集实际分组一致：数据集加宽过（64 → 228 条，新增三个维度），
    # 硬编码清单一旦漏项，接口说明与界面图例就会静默失真（这次正是被数据集同学抓到的）。
    from app.services.ticket_evaluation_service import TicketEvaluationService

    dataset_groups = {case["group"] for case in TicketEvaluationService.load_cases()}
    assert set(body["ticket_groups"]) == dataset_groups, (
        f"分组清单与数据集不一致：清单多 {set(body['ticket_groups']) - dataset_groups}、"
        f"数据集多 {dataset_groups - set(body['ticket_groups'])}"
    )


def test_ticket_eval_run_and_metrics_shape(client, auth):
    resp = client.post(
        "/api/v1/evaluations/tickets/run",
        params={"limit": 6, "groups": "refund", "name": "集成测试-小规模"},
        headers=auth["admin"],
    )
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run["status"] == "completed"
    assert run["dataset_version"] == "tickets-1.1.0"
    # tickets-1.1.0 的 refund 组共 12 条：limit=6 生效，只跑分组内前 6 条
    assert run["case_count"] == len(run["details"]) == 6

    metrics = run["metrics"]
    # 需求书 §9 要求的指标一个都不能少
    for key in (
        "issue_macro_f1",
        "classification_accuracy",
        "action_accuracy",
        "risk_accuracy",
        "approval_gate_recall",
        "expired_policy_leak_rate",
        "policy_version_recall",
        "evidence_support_rate",
        "adapter_failure_handling_rate",
        "tenant_isolation_rate",
        "duplicate_write_blocked_rate",
        "p95_latency_ms",
        "avg_latency_ms",
        "avg_cost_amount",
    ):
        assert key in metrics, f"缺少指标 {key}"

    # 退款组：动作必须与 expect 一致（签收冲突用例期望先核实）、风险 L3、且都进了审批
    refund_cases = [item for item in run["details"] if item["group"] == "refund"]
    assert refund_cases, "refund 组应当有用例"
    for item in refund_cases:
        assert item["observed"]["action_type"] == item["expect"]["action_type"]
        assert item["observed"]["risk_tier"] == "L3"
        assert item["checks"]["gate_requires_approval"] is True
    assert metrics["approval_gate_recall"] == 1.0
    assert metrics["expired_policy_leak_rate"] == 0.0
    assert metrics["total_tokens"] == 0, "stub 模型下不应产生 token 成本"


def test_eval_waits_for_async_dispatch(client, auth, monkeypatch):
    """回归：异步执行后端下，评测必须**等分析跑完**再判分。

    踩过的坑（真实发生）：界面点「运行售后工单评测」时后端用的是默认 threadpool，
    `start_analysis` 只派发就返回，评测立刻判分 —— 8 条用例全部 0 分，而后台
    真的把这 8 次分析跑完了：**钱花了，分还是错的**。现在 `_run_case` 会轮询等终态。
    这里把派发替换成"延迟 0.5 秒后在后台线程执行"，模拟异步后端。
    """
    import threading
    import time

    from app.services import ticket_analysis
    from app.services.workflow_runner import get_runner

    def deferred_dispatch(task_id: str, **_kwargs) -> str:
        def _run() -> None:
            time.sleep(0.5)
            get_runner().run(task_id)

        threading.Thread(target=_run, daemon=True).start()
        return "deferred"

    monkeypatch.setattr(ticket_analysis, "dispatch_task", deferred_dispatch)

    resp = client.post(
        "/api/v1/evaluations/tickets/run",
        params={"limit": 1, "groups": "refund", "name": "集成测试-异步派发"},
        headers=auth["admin"],
    )
    assert resp.status_code == 201, resp.text
    run = resp.json()
    detail = run["details"][0]
    assert detail["observed"]["waited_for_completion"] is True, "必须等到分析终态再判分"
    assert detail["observed"]["action_type"] == "refund", "分析结果必须已经落库"
    assert run["metrics"]["pass_rate"] == 1.0, "异步后端下不该把用例误判成失败"


def test_ticket_eval_run_is_listed_in_history(client, auth):
    resp = client.get("/api/v1/evaluations/runs", params={"limit": 5}, headers=auth["admin"])
    assert resp.status_code == 200
    runs = resp.json()
    assert any(item["dataset_version"] == "tickets-1.1.0" for item in runs), "工单评测必须出现在运行历史里"

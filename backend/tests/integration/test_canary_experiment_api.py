"""集成测试：灰度效果对比接口（权限、口径与"样本不足"行为）。

为什么单独测接口：这套对比是给**做选型决策的人**看的，
接口出错（口径不一致、把样本不足算成结论、越权能看到成本）比模型差更危险。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.db.session import SessionLocal
from app.models.workflow import AgentRun, AnalysisTask, Report


def _seed_group(tenant_id: str, *, canary: int, control: int) -> None:
    """直接写库造两侧样本（不跑工作流，避免慢与花钱）。"""
    session = SessionLocal()
    try:
        for index in range(canary + control):
            source = "canary" if index < canary else "override"
            task = AnalysisTask(
                tenant_id=tenant_id,
                user_id="u-e2e",
                query=f"灰度样本 {index}",
                title=f"灰度样本 {index}",
                status="completed",
            )
            session.add(task)
            session.flush()
            session.add(
                AgentRun(
                    task_id=task.id,
                    agent_code="report_agent",
                    node="report_agent",
                    status="succeeded",
                    model="candidate" if source == "canary" else "baseline",
                    output={
                        "model_source": source,
                        "model_used": True,
                        "schema_ok": True,
                        "findings": [{"text": "x"}] * (4 if source == "canary" else 2),
                    },
                    latency_ms=1000,
                    prompt_tokens=1000,
                    completion_tokens=500,
                    total_tokens=1500,
                    created_at=datetime.utcnow(),
                )
            )
            session.add(
                Report(
                    task_id=task.id,
                    tenant_id=tenant_id,
                    version=1,
                    summary="摘要",
                    facts=[{"text": "f", "evidence_refs": ["E1"]}],
                    confidence=0.7,
                    needs_human_review=False,
                    priority="P1",
                )
            )
        session.commit()
    finally:
        session.close()


def _tenant_id(client, auth) -> str:
    me = client.get("/api/v1/auth/me", headers=auth["pm"]).json()
    return me["tenant_id"]


def test_canary_endpoint_requires_auth(client):
    assert client.get("/api/v1/experiments/canary").status_code == 401


def test_canary_endpoint_reports_insufficient_sample_first(client, auth):
    """还没积累样本时必须明说"样本不足"，而不是给出一个看起来很确定的胜负。"""
    resp = client.get("/api/v1/experiments/canary?days=7", headers=auth["pm"])
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    assert payload["verdict"] == "样本不足"
    assert payload["min_sample"] >= 1
    assert payload["method"]["grouping"]
    assert payload["groups"]["canary"]["task_count"] == 0


def test_canary_endpoint_compares_both_sides(client, auth):
    tenant_id = _tenant_id(client, auth)
    # 用**增量**断言而不是绝对值：集成测试共用同一个库，别的测试（比如售后工单）
    # 也会建任务，绝对计数会被它们按顺序影响。增量同样严格，但不再依赖"库里没有别的任务"。
    before = client.get("/api/v1/experiments/canary?days=1", headers=auth["pm"]).json()
    base_canary = before["groups"]["canary"]["task_count"]
    base_control = before["groups"]["control"]["task_count"]
    base_canary_runs = before["groups"]["canary"]["run_count"]
    base_control_runs = before["groups"]["control"]["run_count"]
    base_canary_findings = before["groups"]["canary"]["avg_findings"]
    base_control_findings = before["groups"]["control"]["avg_findings"]

    _seed_group(tenant_id, canary=6, control=6)

    resp = client.get("/api/v1/experiments/canary?days=1", headers=auth["pm"])
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    canary = payload["groups"]["canary"]
    control = payload["groups"]["control"]
    assert canary["task_count"] == base_canary + 6
    assert control["task_count"] == base_control + 6
    expected_canary = (base_canary_runs * base_canary_findings + 6 * 4) / (base_canary_runs + 6)
    expected_control = (base_control_runs * base_control_findings + 6 * 2) / (base_control_runs + 6)
    # 接口契约会把均值四舍五入到 2 位，因此按响应口径比较，不能拿未舍入值硬比。
    assert canary["avg_findings"] == round(expected_canary, 2)
    assert control["avg_findings"] == round(expected_control, 2)
    assert "candidate" in canary["models"] and "baseline" in control["models"]
    # 灰度侧的"结论条数"显著更高 → 指标层面应判更优
    findings = next(item for item in payload["metrics"] if item["metric"] == "avg_findings")
    assert findings["verdict"] == "灰度更优"
    assert payload["verdict"] in {"灰度更优", "暂无明显差异"}


def test_canary_endpoint_can_filter_by_agent(client, auth):
    tenant_id = _tenant_id(client, auth)
    before = client.get(
        "/api/v1/experiments/canary?days=1&agent_code=metric_agent", headers=auth["pm"]
    ).json()
    _seed_group(tenant_id, canary=3, control=3)

    resp = client.get(
        "/api/v1/experiments/canary?days=1&agent_code=metric_agent", headers=auth["pm"]
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["groups"]["canary"]["run_count"] == before["groups"]["canary"]["run_count"]
    assert payload["groups"]["control"]["run_count"] == before["groups"]["control"]["run_count"]


def test_health_exposes_canary_experiment_threshold(client):
    payload = client.get("/health").json()

    assert "canary_experiment" in payload["llm"]
    assert payload["llm"]["canary_experiment"]["min_sample"] >= 1
    assert "configured" in payload["llm"]["canary_experiment"]

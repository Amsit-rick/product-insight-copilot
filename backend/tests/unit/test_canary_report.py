"""灰度效果对比的单元测试。

这块的核心风险不是"算不出来"，而是**用噪声下结论**：
几个任务的差异、或者一侧样本为 0，都能算出"灰度更优/更差"这种看起来很确定的数字。
所以下面的用例重点钉三件事：

1. 样本不足时只报"样本不足"，不输出任何胜负；
2. 比率差异必须达到统计显著（p < 0.05）才叫更优/更差，趋近的比率给"无显著差异"；
3. 分组口径稳定：只有 `model_source == "canary"` 才算灰度侧，其它（含缺字段）都算对照侧。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.workflow import AgentRun, AnalysisTask, Report
from app.services.canary_report import collect_groups, compare


def make_settings(**overrides) -> Settings:
    base = {
        "llm_provider": "stub",
        "model_canary": {"report_agent": {"model": "candidate-model", "percent": 50}},
        "canary_min_sample": 3,
        "model_prices": {"candidate-model": {"input": 1000.0, "output": 1000.0}},
        "model_price_default": {"input": 1000.0, "output": 1000.0},
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def add_task(session: Session, tenant_id: str = "t-canary") -> AnalysisTask:
    task = AnalysisTask(
        tenant_id=tenant_id,
        user_id="u-1",
        query="灰度对比用例",
        title="灰度对比用例",
        status="completed",
    )
    session.add(task)
    session.flush()
    return task


def add_run(
    session: Session,
    task: AnalysisTask,
    *,
    source: str | None,
    model: str = "candidate-model",
    model_used: bool = True,
    schema_ok: bool = True,
    findings: int = 3,
    status: str = "succeeded",
    latency_ms: int = 1000,
    tokens: int = 1000,
    cache_hit: int = 0,
    agent_code: str = "report_agent",
) -> AgentRun:
    output: dict = {
        "findings": [{"text": f"结论{i}"} for i in range(findings)],
        "model_used": model_used,
        "schema_ok": schema_ok,
        "cache_hit_tokens": cache_hit,
    }
    if source is not None:
        output["model_source"] = source
    run = AgentRun(
        task_id=task.id,
        agent_code=agent_code,
        node=agent_code,
        status=status,
        model=model,
        output=output,
        latency_ms=latency_ms,
        prompt_tokens=tokens,
        completion_tokens=0,
        total_tokens=tokens,
        created_at=datetime.utcnow(),
    )
    session.add(run)
    session.flush()
    return run


def add_report(
    session: Session,
    task: AnalysisTask,
    *,
    facts: int = 5,
    cited: bool = True,
    conflicts: int = 0,
    human: bool = False,
    confidence: float = 0.7,
) -> Report:
    report = Report(
        task_id=task.id,
        tenant_id=task.tenant_id,
        version=1,
        summary="摘要",
        facts=[{"text": f"事实{i}", "evidence_refs": ["E1"] if cited else []} for i in range(facts)],
        conflicts=[{"topic": f"冲突{i}"} for i in range(conflicts)],
        confidence=confidence,
        needs_human_review=human,
        priority="P1",
    )
    session.add(report)
    session.flush()
    return report


def seed_pair(session: Session, *, canary_tasks: int, control_tasks: int, **kwargs) -> None:
    for _ in range(canary_tasks):
        task = add_task(session)
        add_run(session, task, source="canary", **kwargs)
        add_report(session, task)
    for _ in range(control_tasks):
        task = add_task(session)
        add_run(session, task, source="override", **kwargs)
        add_report(session, task)


# --------------------------------------------------------------------- 分组口径
def test_只有_canary_算灰度侧(session):
    task = add_task(session)
    add_run(session, task, source="canary")
    add_run(session, task, source="override")
    add_run(session, task, source="global")
    add_run(session, task, source=None)  # 老数据没有该字段

    groups = collect_groups(session, "t-canary", settings=make_settings())

    assert groups["canary"].run_count == 1
    assert groups["control"].run_count == 3


def test_统计口径正确(session):
    task = add_task(session)
    add_run(session, task, source="canary", findings=4, latency_ms=800, tokens=2000, model_used=True)
    add_run(session, task, source="canary", findings=2, model_used=False, schema_ok=False, status="failed")
    add_report(session, task, facts=4, cited=True)

    groups = collect_groups(session, "t-canary", settings=make_settings())
    canary = groups["canary"]

    assert canary.task_count == 1
    assert canary.run_count == 2
    assert canary.model_calls == 1
    assert canary.model_used_rate == pytest.approx(0.5)
    assert canary.schema_ok_rate == pytest.approx(0.5)
    assert canary.error_rate == pytest.approx(0.5)
    assert canary.avg_findings == pytest.approx(3.0)
    assert canary.total_tokens == 3000  # 2000 + 默认 1000
    assert canary.report_count == 1
    assert canary.avg_facts_per_report == pytest.approx(4.0)
    assert canary.cited_fact_rate == pytest.approx(1.0)


def test_只统计窗口内与指定租户(session):
    old_task = add_task(session)
    add_run(session, old_task, source="canary")
    other = add_task(session, tenant_id="t-other")
    add_run(session, other, source="canary")
    old_task.created_at = datetime(2020, 1, 1)
    # 会话是 autoflush=False（项目有意为之，避免隐式写入），所以改动要显式 flush 才入查询视野
    session.flush()

    groups = collect_groups(session, "t-canary", days=7, settings=make_settings())

    assert groups["canary"].run_count == 0  # 窗口外 + 别的租户都不算


def test_按_agent_过滤(session):
    task = add_task(session)
    add_run(session, task, source="canary", agent_code="report_agent")
    add_run(session, task, source="canary", agent_code="metric_agent")

    groups = collect_groups(
        session, "t-canary", agent_code="report_agent", settings=make_settings()
    )

    assert groups["canary"].run_count == 1


# ------------------------------------------------------------------- 样本量门槛
def test_样本不足时不给胜负结论(session):
    seed_pair(session, canary_tasks=1, control_tasks=1)

    result = compare(session, "t-canary", settings=make_settings())

    assert result["verdict"] == "样本不足"
    assert "各 ≥ 3" in result["verdict_reason"]
    # 样本不足时所有指标项也都不应给"更优/更差"
    for item in [*result["metrics"], *result["report_metrics"]]:
        assert item["verdict"] in {"样本不足", "无显著差异"}


def test_一侧为空时明确说明是哪一侧(session):
    seed_pair(session, canary_tasks=5, control_tasks=0)

    result = compare(session, "t-canary", settings=make_settings())

    assert result["verdict"] == "样本不足"
    assert "对照侧还没有任务" in result["verdict_reason"]


def test_未配置灰度且无样本时说明原因(session):
    seed_pair(session, canary_tasks=5, control_tasks=5)

    result = compare(session, "t-canary", settings=make_settings(model_canary={}))

    # 有样本就能比：灰度配置撤掉后仍要能读到历史结论
    assert result["canary_configured"] is False
    assert result["verdict"] in {"灰度更优", "灰度更差", "暂无明显差异"}
    assert "历史流量" in result["verdict_reason"]


def test_没有任何样本时说明是未配置还是没任务(session):
    result = compare(session, "t-canary", settings=make_settings(model_canary={}))

    assert result["verdict"] == "样本不足"
    assert "未配置 MODEL_CANARY" in result["verdict_reason"]


# --------------------------------------------------------------------- 结论判定
def test_显著差异时给出更优结论(session):
    """灰度侧 model_used 100%（10/10），对照侧 50%（5/10）→ 应判"灰度更优"。"""
    for _ in range(10):
        task = add_task(session)
        add_run(session, task, source="canary", model_used=True)
        add_report(session, task)
    for index in range(10):
        task = add_task(session)
        add_run(session, task, source="override", model_used=index < 5)
        add_report(session, task)

    result = compare(session, "t-canary", settings=make_settings())
    metric = next(item for item in result["metrics"] if item["metric"] == "model_used_rate")

    assert metric["verdict"] == "灰度更优"
    assert metric["p_value"] is not None and metric["p_value"] < 0.05
    assert result["verdict"] == "灰度更优"


def test_趋近的比率判为无显著差异(session):
    """10/10 vs 9/10：差 10%，但样本量下不显著 → 不能宣称更优。"""
    for _ in range(10):
        task = add_task(session)
        add_run(session, task, source="canary", model_used=True)
        add_report(session, task)
    for index in range(10):
        task = add_task(session)
        add_run(session, task, source="override", model_used=index < 9)
        add_report(session, task)

    result = compare(session, "t-canary", settings=make_settings())
    metric = next(item for item in result["metrics"] if item["metric"] == "model_used_rate")

    assert metric["verdict"] == "无显著差异"
    assert metric["p_value"] >= 0.05


def test_均值类指标用相对差阈值(session):
    """成本/耗时差异小于 10% 时不该说"更优/更差"。"""
    seed_pair(session, canary_tasks=3, control_tasks=3, latency_ms=1000)
    task = next(
        run.task_id
        for run in session.query(AgentRun).filter(AgentRun.agent_code == "report_agent").all()
    )
    for run in session.query(AgentRun).filter(AgentRun.task_id == task).all():
        run.output = {**(run.output or {}), "model_source": "canary"}
    session.flush()

    result = compare(session, "t-canary", settings=make_settings())
    latency = next(item for item in result["metrics"] if item["metric"] == "latency_p95_ms")

    assert latency["verdict"] == "无显著差异"
    assert "启发式" in latency["note"]


def test_成本按侧分别核算(session):
    """两侧模型不同价：成本必须各算各的，否则"便宜"就白看了。"""
    settings = make_settings(
        model_prices={
            "candidate-model": {"input": 1000.0, "output": 1000.0},
            "control-model": {"input": 10.0, "output": 10.0},
        }
    )
    task = add_task(session)
    add_run(session, task, source="canary", model="candidate-model", tokens=1000)
    add_run(session, task, source="override", model="control-model", tokens=1000)

    groups = collect_groups(session, "t-canary", settings=settings)

    assert groups["canary"].total_cost == pytest.approx(1.0)  # 1000 token × 1000/1e6
    assert groups["control"].total_cost == pytest.approx(0.01)
    assert groups["canary"].cost_per_task == pytest.approx(1.0)


def test_返回方法与局限说明(session):
    """结论必须自带"怎么算的"与"不能拿来干什么"，避免被当成严格 A/B 结论引用。"""
    seed_pair(session, canary_tasks=3, control_tasks=3)

    result = compare(session, "t-canary", settings=make_settings())

    assert result["method"]["grouping"]
    assert result["method"]["rates"]
    assert len(result["method"]["limitations"]) >= 3
    assert result["min_sample"] == 3

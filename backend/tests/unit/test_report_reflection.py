"""产品诊断报告反思闭环的规则层测试。"""

from __future__ import annotations

from app.agents.report_reflection import ReportReflectionAgent, ReportRevisionAgent
from app.agents.schemas import AgentInput
from app.config import Settings
from app.services.report_reflection import inspect_report, revise_report


def _settings() -> Settings:
    return Settings(_env_file=None, llm_provider="stub", reflection_enabled=True)


def _input(**filters) -> AgentInput:
    return AgentInput(
        session=None,
        task_id="task-report-1",
        tenant_id="demo",
        user_id="u1",
        query="为什么版本转化率下降？",
        filters=filters,
        kb_codes=set(),
    )


def _report() -> dict:
    return {
        "summary": "版本转化率下降，原因仍需核实。",
        "facts": [
            {
                "id": "fact-1",
                "type": "fact",
                "text": "3.2.0 转化率下降 8%",
                "evidence_refs": [],
                "confidence": 0.8,
            }
        ],
        "inferences": [],
        "hypotheses": [],
        "recommendations": [],
        "conflicts": [],
        "priority": "P1",
        "confidence": 0.7,
    }


def test_report_reflection_marks_missing_citation_as_revisable():
    result = inspect_report(report=_report(), evidence_ids={"E1"}, conflicts=[])

    assert result.verdict == "revise"
    assert any(item.code == "statement_missing_citation" for item in result.issues)


def test_report_revision_demotes_unreferenced_fact_and_creates_new_structure():
    reflection = inspect_report(report=_report(), evidence_ids={"E1"}, conflicts=[])
    revision = revise_report(
        report=_report(),
        issues=[item.model_dump() for item in reflection.issues],
        evidence_ids={"E1"},
        conflicts=[],
    )

    assert revision.revised_report.facts == []
    assert revision.revised_report.hypotheses
    assert revision.revised_report.hypotheses[0]["verify_by"]
    assert "statement_missing_citation" in revision.addressed_issue_codes


def test_report_reflection_and_revision_agents_have_zero_tool_budget():
    reflection = ReportReflectionAgent(settings=_settings())
    revision = ReportRevisionAgent(settings=_settings())

    assert reflection.tool_budget(_input()) == 0
    assert revision.tool_budget(_input()) == 0


def test_report_reflection_agent_reads_current_draft():
    from app.agents.base import Collected

    output = ReportReflectionAgent(settings=_settings()).deterministic_output(
        _input(report_draft=_report(), report_evidence_ids=["E1"], report_conflicts=[]),
        Collected(),
    )

    assert output["verdict"] == "revise"
    assert output["issues"][0]["target"] == "diagnostic_report"


def test_reflection_agents_use_dedicated_timeout_and_token_limits():
    settings = _settings().model_copy(
        update={"reflection_timeout_seconds": 17.0, "reflection_max_tokens": 900}
    )
    agent = ReportReflectionAgent(settings=settings)

    assert agent.timeout_seconds(_input()) == 17.0
    assert agent.max_model_tokens(_input()) == 900


def test_reflection_agent_combines_task_and_reflection_budget(monkeypatch):
    settings = _settings().model_copy(update={"reflection_cost_limit": 0.25})
    agent = ReportReflectionAgent(settings=settings)

    monkeypatch.setattr(
        "app.services.cost.budget_state",
        lambda session, task_id, settings: {
            "exceeded": False,
            "reasons": [],
            "action": "degrade",
            "limits": {},
        },
    )
    monkeypatch.setattr(
        "app.services.cost.reflection_budget_state",
        lambda session, task_id, settings: {
            "exceeded": True,
            "reasons": ["反思成本已达专属上限"],
            "action": "degrade",
            "limits": {"reflection_cost_limit": 0.25},
        },
    )

    state = agent.budget_check(_input())

    assert state["exceeded"] is True
    assert state["reasons"] == ["反思成本已达专属上限"]

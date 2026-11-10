from types import SimpleNamespace

from app.services.ticket_view import build_analysis


def _ticket():
    return SimpleNamespace(
        last_analysis_task_id="task-1",
        analysis_count=1,
        issue_type=None,
        subject="申请退款",
        content="商品不想要了，申请退款。",
        risk_tier="L3",
    )


def _runs(codes):
    return [{"agent_code": code, "status": "succeeded", "model": "stub", "total_tokens": 0} for code in codes]


def test_build_analysis_infers_legacy_multi_agent_mode_from_actual_runs():
    codes = [
        "triage_agent",
        "order_fact_agent",
        "policy_rag_agent",
        "resolution_agent",
        "response_agent",
        "review_agent",
        "followup_agent",
    ]

    result = build_analysis(
        _ticket(), None, agent_runs=_runs(codes), task_filters={}, task_status="completed"
    )

    assert result["mode"] == "multi_agent"
    assert result["mode_label"] == "多 Agent 协作"
    assert result["agent_count"] == 7
    assert len(result["business_steps"]) == 7
    assert "退款" in result["route_reason"]
    assert result["steps"] == []
    assert "analysis_task_id" not in result


def test_build_analysis_infers_fast_path_for_legacy_four_agent_runs():
    result = build_analysis(
        _ticket(),
        None,
        agent_runs=_runs(
            ["triage_agent", "order_fact_agent", "policy_rag_agent", "review_agent"]
        ),
        task_filters={},
        task_status="completed",
    )

    assert result["mode"] == "fast_path"
    assert result["agent_count"] == 4
    assert len(result["business_steps"]) == 4
    assert all(step["status"] == "succeeded" for step in result["business_steps"])


def test_build_analysis_only_returns_technical_details_when_authorized():
    result = build_analysis(
        _ticket(),
        None,
        agent_runs=_runs(["triage_agent"]),
        task_filters={"multi_agent": False},
        task_status="running",
        can_view_technical=True,
    )

    assert result["analysis_task_id"] == "task-1"
    assert result["steps"][0]["model"] == "stub"
    assert result["business_steps"][0]["status"] == "succeeded"
    assert result["business_steps"][1]["status"] == "pending"


def test_build_analysis_marks_unreached_roles_skipped_for_completed_task():
    result = build_analysis(
        _ticket(),
        None,
        agent_runs=_runs(["triage_agent"]),
        task_filters={"multi_agent": False},
        task_status="completed",
    )

    assert [step["status"] for step in result["business_steps"]] == [
        "succeeded",
        "skipped",
        "skipped",
        "skipped",
    ]
    assert result["agent_count"] == 1


def test_build_analysis_does_not_claim_policy_found_when_references_are_empty():
    ticket = _ticket()
    ticket.subject = "满减优惠未生效"
    ticket.content = "满减优惠没有生效，请解释优惠规则。"
    recommendation = SimpleNamespace(policy_refs=[], confidence=None, risk_tier="L2")

    result = build_analysis(
        ticket,
        recommendation,
        agent_runs=_runs(
            ["triage_agent", "order_fact_agent", "policy_rag_agent", "review_agent"]
        ),
        task_filters={"multi_agent": False, "issue_type": "promotion_not_applied"},
        task_status="completed",
    )

    policy_step = next(step for step in result["business_steps"] if step["agent_code"] == "policy_rag_agent")
    assert policy_step["conclusion"] == "未找到适用规则，不能据此下结论"

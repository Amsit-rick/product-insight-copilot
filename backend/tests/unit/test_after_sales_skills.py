from app.models.enums import IssueType
from app.skills.after_sales import (
    list_agent_skills,
    select_business_skills,
    skill_prompt_for_agent,
)


def test_business_skills_are_selected_deterministically_for_supported_scenarios():
    assert [item["code"] for item in select_business_skills(IssueType.PROMOTION_NOT_APPLIED.value, False)] == [
        "promotion_explanation"
    ]
    assert [item["code"] for item in select_business_skills(IssueType.REFUND.value, True)] == [
        "refund_review",
        "quality_guard",
    ]
    assert [item["code"] for item in select_business_skills(IssueType.LOGISTICS_STALLED.value, True)] == [
        "logistics_exception",
        "quality_guard",
    ]
    assert [item["code"] for item in select_business_skills(IssueType.MISSING_ITEM.value, True)] == [
        "fulfillment_exception",
        "quality_guard",
    ]
    assert [item["code"] for item in select_business_skills(IssueType.DAMAGED.value, True)] == [
        "damage_evidence",
        "quality_guard",
    ]
    assert [item["code"] for item in select_business_skills(IssueType.OTHER.value, True)] == [
        "quality_guard"
    ]


def test_business_skills_only_provide_relevant_agent_guidance():
    skills = select_business_skills(IssueType.REFUND.value, True)

    resolution_guidance = skill_prompt_for_agent("resolution_agent", skills)
    assert "实付金额" in resolution_guidance
    assert "不得改变规则决定的动作、金额" in resolution_guidance
    assert "物流停滞" not in resolution_guidance
    assert skill_prompt_for_agent("triage_agent", skills) == ""

    review_guidance = skill_prompt_for_agent("review_agent", skills)
    assert "退款" in review_guidance
    assert "回复承诺" in review_guidance


def test_skill_registry_metadata_has_versions_and_no_write_authority():
    registry = list_agent_skills()

    assert {item["code"] for item in registry} == {
        "promotion_explanation",
        "refund_review",
        "logistics_exception",
        "fulfillment_exception",
        "damage_evidence",
        "quality_guard",
    }
    assert all(item["version"] for item in registry)
    assert all(item["allowed_actions"] == [] for item in registry)
    assert all(item["may_change_decision"] is False for item in registry)

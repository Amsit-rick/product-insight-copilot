from __future__ import annotations

from app.config import Settings
from app.services.reflection_rollout import (
    ReflectionRolloutRequest,
    decide_reflection_rollout,
)


def make_settings(**overrides) -> Settings:
    base = {
        "llm_provider": "stub",
        "reflection_enabled": True,
        "reflection_ticket_enabled": True,
        "reflection_report_enabled": True,
        "reflection_rollout_percent": 25,
        "reflection_rollout_salt": "stable-test-salt",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def request(**overrides) -> ReflectionRolloutRequest:
    base = {
        "tenant_id": "tenant-a",
        "task_id": "task-001",
        "target": "ticket_reply",
        "risk_level": "L3",
        "issue_type": "refund",
    }
    base.update(overrides)
    return ReflectionRolloutRequest(**base)


def test_same_task_is_always_assigned_to_same_bucket():
    settings = make_settings()

    first = decide_reflection_rollout(request(), settings=settings)
    second = decide_reflection_rollout(request(), settings=settings)

    assert first.bucket == second.bucket
    assert first.enabled == second.enabled
    assert first.percent == 25


def test_changing_salt_reassigns_at_least_one_stable_task():
    original = make_settings(reflection_rollout_salt="salt-a")
    changed = make_settings(reflection_rollout_salt="salt-b")

    buckets_a = {
        task_id: decide_reflection_rollout(request(task_id=task_id), settings=original).bucket
        for task_id in (f"task-{index}" for index in range(20))
    }
    buckets_b = {
        task_id: decide_reflection_rollout(request(task_id=task_id), settings=changed).bucket
        for task_id in buckets_a
    }

    assert any(buckets_a[task_id] != buckets_b[task_id] for task_id in buckets_a)


def test_scope_mismatch_disables_reflection_before_bucketing():
    settings = make_settings(
        reflection_rollout_tenants=["tenant-b"],
        reflection_rollout_risk_levels=["L4"],
        reflection_rollout_issue_types=["damaged"],
    )

    decision = decide_reflection_rollout(request(), settings=settings)

    assert decision.enabled is False
    assert decision.bucket is None
    assert decision.reason == "tenant_not_in_scope"


def test_rolled_back_release_state_forces_reflection_off():
    decision = decide_reflection_rollout(
        request(),
        settings=make_settings(reflection_rollout_percent=100),
        release_state={"status": "rolled_back", "current_percent": 100},
    )

    assert decision.enabled is False
    assert decision.percent == 0
    assert decision.release_status == "rolled_back"
    assert decision.reason == "release_rolled_back"


def test_server_decision_overwrites_request_side_enable_flag():
    settings = make_settings(reflection_rollout_percent=0)
    filters = {"reflection_enabled": True, "max_reflection_rounds": 99}

    decision = decide_reflection_rollout(request(), settings=settings)
    filters["reflection_enabled"] = decision.enabled
    filters["max_reflection_rounds"] = settings.max_reflection_rounds
    filters["reflection_rollout"] = decision.as_dict()

    assert filters["reflection_enabled"] is False
    assert filters["max_reflection_rounds"] == settings.max_reflection_rounds
    assert filters["reflection_rollout"]["reason"] == "control_group"


from __future__ import annotations

from app.config import Settings
from app.services.reflection_release import ReflectionReleaseMetrics, evaluate_reflection_release


def make_settings(**overrides) -> Settings:
    base = {
        "llm_provider": "stub",
        "reflection_release_min_sample": 20,
        "reflection_release_error_rate_limit": 0.05,
        "reflection_release_bounded_termination_min": 0.98,
        "reflection_release_latency_multiplier": 1.5,
        "reflection_release_token_multiplier": 1.5,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def metrics(**overrides) -> ReflectionReleaseMetrics:
    base = {
        "treatment_sample": 30,
        "control_sample": 30,
        "treatment_error_rate": 0.01,
        "control_error_rate": 0.02,
        "safety_violation_rate": 0.0,
        "protected_field_violation_rate": 0.0,
        "bounded_termination_rate": 1.0,
        "treatment_p95_latency_ms": 1200.0,
        "control_p95_latency_ms": 1000.0,
        "treatment_avg_tokens": 1200.0,
        "control_avg_tokens": 1000.0,
        "treatment_human_review_rate": 0.1,
        "control_human_review_rate": 0.12,
    }
    base.update(overrides)
    return ReflectionReleaseMetrics(**base)


def test_insufficient_samples_hold_current_percent_without_claiming_improvement():
    result = evaluate_reflection_release(
        metrics(treatment_sample=19), current_percent=10, settings=make_settings()
    )

    assert result.recommendation == "hold"
    assert result.recommended_percent == 10
    assert result.requires_human_approval is False
    assert "样本不足" in result.reason


def test_safety_or_bounded_termination_failure_triggers_automatic_rollback():
    result = evaluate_reflection_release(
        metrics(safety_violation_rate=0.01, bounded_termination_rate=0.95),
        current_percent=25,
        settings=make_settings(),
    )

    assert result.recommendation == "rollback"
    assert result.recommended_percent == 0
    assert result.auto_apply is True
    assert result.requires_human_approval is False


def test_latency_or_token_regression_pauses_expansion():
    result = evaluate_reflection_release(
        metrics(treatment_p95_latency_ms=1800, treatment_avg_tokens=1700),
        current_percent=25,
        settings=make_settings(),
    )

    assert result.recommendation == "pause"
    assert result.recommended_percent == 25
    assert result.auto_apply is False
    assert result.requires_human_approval is False


def test_healthy_release_recommends_next_step_but_requires_admin_approval():
    result = evaluate_reflection_release(metrics(), current_percent=25, settings=make_settings())

    assert result.recommendation == "promote"
    assert result.recommended_percent == 50
    assert result.auto_apply is False
    assert result.requires_human_approval is True


def test_release_never_recommends_more_than_full_coverage():
    result = evaluate_reflection_release(metrics(), current_percent=100, settings=make_settings())

    assert result.recommendation == "hold"
    assert result.recommended_percent == 100
    assert result.requires_human_approval is False

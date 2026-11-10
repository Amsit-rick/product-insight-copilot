"""可回答性判定合并逻辑的单测（规则 vs 模型复核）。

这段逻辑直接决定"报告里有没有事实结论"，因此必须有单测钉住三条边界：

1. 规则强（覆盖率/相关度到高分档）→ 模型**不能**推翻，否则正常问题会被误判成无答案；
2. 规则弱 → 由模型裁决（这才是模型擅长的"问法改写 vs 库里没有"那一段）；
3. 没有模型（离线 stub）→ 完全按规则走，行为与接入模型前一致。
"""

from __future__ import annotations

from app.config import Settings
from app.graph.nodes.evidence import combine_answerability


def make_settings(**overrides) -> Settings:
    base = {
        "llm_provider": "stub",
        "answerability_min_coverage": 0.2,
        "answerability_high_score": 0.26,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def rule(**overrides) -> dict:
    payload = {
        "coverage": 0.4,
        "threshold": 0.2,
        "top_score": 0.1,
        "high_score_escape": 0.26,
        "answerable": True,
        "missing_terms": [],
    }
    payload.update(overrides)
    return payload


def test_规则强时模型不能推翻():
    """实测场景：46 条证据、覆盖率 40%，模型却判"证据不足"→ 必须维持可回答。"""
    answerability = rule(coverage=0.40)
    verdict = {"answerable": False, "reason": "证据不足", "model": "deepseek-chat"}

    decided_by = combine_answerability(answerability, verdict, make_settings())

    assert decided_by == "rule"
    assert answerability["answerable"] is True
    assert answerability["model_overridden"] is False
    assert answerability["rule_answerable"] is True


def test_相关度高分同样算规则强():
    answerability = rule(coverage=0.05, top_score=0.9)
    verdict = {"answerable": False, "reason": "证据不足"}

    assert combine_answerability(answerability, verdict, make_settings()) == "rule"
    assert answerability["answerable"] is True


def test_规则弱时由模型裁决_模型否定():
    answerability = rule(coverage=0.10, top_score=0.05)

    decided_by = combine_answerability(
        answerability, {"answerable": False, "reason": "库里没有该主题"}, make_settings()
    )

    assert decided_by == "model"
    assert answerability["answerable"] is False
    assert answerability["model_overridden"] is True
    assert "库里没有该主题" in answerability["reason"]


def test_规则弱时由模型裁决_模型救回改写的问法():
    """规则因分词/改写判不足，模型认出"其实问的是同一件事"→ 可回答。"""
    answerability = rule(coverage=0.08, top_score=0.0, answerable=False)

    decided_by = combine_answerability(
        answerability, {"answerable": True, "reason": "改写问法，证据相关"}, make_settings()
    )

    assert decided_by == "model"
    assert answerability["answerable"] is True


def test_没有模型时完全按规则走():
    answerability = rule(coverage=0.05, answerable=False)

    assert combine_answerability(answerability, None, make_settings()) == "rule"
    assert answerability["answerable"] is False
    assert answerability["decided_by"] == "rule"


def test_规则强但本身判定不足时不强行救回():
    """覆盖率达标但规则因其它条件判不足：此时模型仍可复核（不能反过来变成"一定可回答"）。"""
    answerability = rule(coverage=0.40, answerable=False)

    decided_by = combine_answerability(
        answerability, {"answerable": True, "reason": "证据足够"}, make_settings()
    )

    assert decided_by == "model"
    assert answerability["answerable"] is True

"""意图识别（`app.services.intent`）单元测试。

规则优先的理由是"可解释、可测试、零延迟"：这里把设计文档里列的五类意图
各挑一组典型问法，验证分类结果与置信度区间。
"""

from __future__ import annotations

import pytest

from app.models.enums import IntentCode
from app.services.intent import classify_intent_rules, looks_like_version, score_intents

VERSION = IntentCode.VERSION_IMPACT.value
METRIC = IntentCode.METRIC_ANOMALY.value
FEEDBACK = IntentCode.FEEDBACK_CLUSTER.value
PRIORITY = IntentCode.REQUIREMENT_PRIORITY.value
GENERAL = IntentCode.GENERAL_QA.value


# --------------------------------------------------------------------------- 五类意图
@pytest.mark.parametrize(
    "query",
    [
        "3.2.0 版本发布了哪些变更",
        "上个版本上线后有没有回归问题",
        "这次灰度发布和回滚的相关性分析",
        "请对比 3.1.0 与 3.2.0 的发版变更",
    ],
)
def test_版本类问题归为版本影响(query: str):
    intent, confidence, reason = classify_intent_rules(query)

    assert intent == VERSION
    assert 0.4 <= confidence <= 0.95
    assert "关键词得分" in reason


@pytest.mark.parametrize(
    "query",
    [
        "为什么留存下降了",
        "次日留存环比下跌，是不是指标异常",
        "崩溃率为什么掉得这么厉害",
        "上周 GMV 同比波动的原因",
    ],
)
def test_指标下跌类问题归为指标异常(query: str):
    intent, confidence, _ = classify_intent_rules(query)

    assert intent == METRIC
    assert 0.4 <= confidence <= 0.95


@pytest.mark.parametrize(
    "query",
    [
        "把最近的用户反馈做主题聚类",
        "这批工单和差评能不能聚成几类",
        "整理用户声音里的核心主题",
    ],
)
def test_反馈聚类类问题归为反馈聚类(query: str):
    intent, confidence, _ = classify_intent_rules(query)

    assert intent == FEEDBACK
    assert 0.4 <= confidence <= 0.95


@pytest.mark.parametrize(
    "query",
    [
        "这个需求优先级怎么排期",
        "两个需求做不做，怎么取舍",
        "先做哪个需求的 ROI 更高",
    ],
)
def test_优先级类问题归为需求优先级(query: str):
    intent, confidence, _ = classify_intent_rules(query)

    assert intent == PRIORITY
    assert 0.4 <= confidence <= 0.95


@pytest.mark.parametrize(
    "query",
    ["今天天气怎么样", "帮我写一段自我介绍", "公司的报销流程是什么"],
)
def test_无关问题退化为通用问答(query: str):
    intent, confidence, reason = classify_intent_rules(query)

    assert intent == GENERAL
    assert confidence == 0.4
    assert reason == "未命中任何意图关键词，退化为通用问答"


# --------------------------------------------------------------------------- 边界规则
def test_只在问题里出现版本号时按版本影响处理():
    """没有任何关键词、但出现了 x.y.z：按版本影响分析，置信度给 0.55。"""
    intent, confidence, reason = classify_intent_rules("帮我看看 3.2.0")

    assert intent == VERSION
    assert confidence == 0.55
    assert reason == "问题中出现版本号，按版本影响分析处理"


def test_版本号与指标词同时出现时以关键词得分为准():
    """版本关键词得分（5.2）高于"留存"（2.0），判为版本影响。"""
    intent, _, reason = classify_intent_rules("3.2.0 版本发布后的留存相关性")

    assert intent == VERSION
    assert "关键词得分" in reason


def test_具体词权重更高_留存下降压过版本():
    """"留存 + 下降"（3.6 分）应高于只提到"版本"（2.0 分）。"""
    scores = score_intents("版本 留存下降")

    assert scores[METRIC] > scores[VERSION]
    intent, _, _ = classify_intent_rules("版本 留存下降")
    assert intent == METRIC


def test_置信度上限为0_95():
    intent, confidence, _ = classify_intent_rules("留存下降 异常 指标 崩溃率 转化率 为什么掉")

    assert intent == METRIC
    assert confidence == 0.95


def test_置信度随得分与领先幅度上升():
    """两个意图得分接近时置信度低；单一意图大幅领先时置信度高。"""
    weak_intent, weak, _ = classify_intent_rules("反馈和需求哪个更重要")
    strong_intent, strong, _ = classify_intent_rules("把用户反馈做主题聚类")

    assert weak_intent == FEEDBACK  # 同为 2.0 分，按关键词表顺序取前者
    assert strong_intent == FEEDBACK
    assert weak < strong
    assert 0.4 <= weak <= 0.95
    assert 0.4 <= strong <= 0.95


def test_空问题退化为通用问答():
    assert classify_intent_rules("") == (GENERAL, 0.4, "未命中任何意图关键词，退化为通用问答")
    assert classify_intent_rules(None) == (GENERAL, 0.4, "未命中任何意图关键词，退化为通用问答")


def test_命中词会写进判断依据():
    _, _, reason = classify_intent_rules("把用户反馈做聚类")

    assert "反馈" in reason and "聚类" in reason


@pytest.mark.parametrize(
    ("text", "expected"),
    [("3.2.0", True), ("v3.2.0-beta", True), ("版本三十二", False), ("", False), ("3.2", True)],
)
def test_版本号正则(text: str, expected: bool):
    assert looks_like_version(text) is expected


def test_大小写不敏感():
    assert classify_intent_rules("GMV 下跌了")[0] == METRIC
    assert classify_intent_rules("gmv 下跌了")[0] == METRIC


def test_得分表只保留命中项():
    scores = score_intents("留存下降")

    assert set(scores) == {METRIC}
    assert scores[METRIC] == pytest.approx(3.6)

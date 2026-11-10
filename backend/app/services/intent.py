"""意图识别（规则优先，可选模型复核）。

为什么先用规则：意图只决定"取数范围"，用规则实现可解释、可测试、零延迟；
模型只做复核，且校验通过才采纳（见 `graph/nodes/intake.py`）。
"""

from __future__ import annotations

from app.models.enums import IntentCode

# 关键词权重：越具体的词权重越高，避免"版本"两个字就压过"留存下降"
INTENT_KEYWORDS: dict[str, dict[str, float]] = {
    IntentCode.METRIC_ANOMALY.value: {
        "留存": 2.0, "下降": 1.6, "下跌": 1.6, "异常": 1.8, "波动": 1.2, "环比": 1.2,
        "同比": 1.2, "指标": 1.6, "转化率": 1.4, "崩溃率": 1.4, "活跃": 1.2, "gmv": 1.4,
        "营收": 1.4, "为什么掉": 2.0, "漏损": 1.6,
    },
    IntentCode.VERSION_IMPACT.value: {
        "版本": 2.0, "发布": 1.8, "上线": 1.6, "升级": 1.6, "灰度": 1.6, "回滚": 1.6,
        "发版": 1.8, "changelog": 1.6, "变更": 1.6, "相关性": 1.4, "回归": 1.2,
    },
    IntentCode.FEEDBACK_CLUSTER.value: {
        "反馈": 2.0, "工单": 1.8, "评论": 1.6, "投诉": 1.8, "聚类": 2.0, "主题": 1.6,
        "用户声音": 1.8, "差评": 1.6, "访谈": 1.4, "问卷": 1.4,
    },
    IntentCode.REQUIREMENT_PRIORITY.value: {
        "需求": 2.0, "优先级": 2.0, "排期": 1.6, "做不做": 1.8, "取舍": 1.6,
        "roi": 1.4, "该不该": 1.6, "先做": 1.6, "价值": 1.2,
    },
}

VERSION_PATTERN_WEIGHT = 1.2
DEFAULT_INTENT = IntentCode.GENERAL_QA.value


def score_intents(query: str) -> dict[str, float]:
    text = (query or "").lower()
    scores: dict[str, float] = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        total = 0.0
        for keyword, weight in keywords.items():
            if keyword in text:
                total += weight
        if total:
            scores[intent] = round(total, 3)
    return scores


def looks_like_version(text: str) -> bool:
    import re

    return bool(re.search(r"\d+\.\d+(\.\d+)?", text or ""))


def classify_intent_rules(query: str) -> tuple[str, float, str]:
    """返回 (intent, confidence, reason)。"""
    scores = score_intents(query)
    if not scores:
        if looks_like_version(query):
            return (
                IntentCode.VERSION_IMPACT.value,
                0.55,
                "问题中出现版本号，按版本影响分析处理",
            )
        return DEFAULT_INTENT, 0.4, "未命中任何意图关键词，退化为通用问答"

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    intent, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    # 命中越多、与第二名差距越大，置信度越高
    confidence = min(0.95, 0.45 + 0.1 * top + 0.15 * (top - second))
    if looks_like_version(query) and intent == IntentCode.METRIC_ANOMALY.value:
        confidence = min(0.95, confidence + 0.03)
    reason = f"关键词得分 {top:.1f}（次高 {second:.1f}）：{_hits(query, intent)}"
    return intent, round(confidence, 2), reason


def _hits(query: str, intent: str) -> str:
    text = (query or "").lower()
    hits = [k for k in INTENT_KEYWORDS.get(intent, {}) if k in text]
    return "、".join(hits[:5]) or "无"

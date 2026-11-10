"""覆盖率"信息词"过滤的单测（问句外壳必须从分母里剔除）。

## 为什么需要这些测试

覆盖率直接决定"报告有没有事实结论"，而它的分母是**字符二元组**（中文没分词器）。
问句的口语外壳（`帮我`/`归一`/`哪几`/`多的`）会混进分母，且永远匹配不上任何文档，
把本该可回答的问题压到阈值以下 —— 实测有一条真实用例因此被误拒（详见
`app/services/answerability.py` 的模块注释与 `scripts/sweep_coverage_rules.py`）。

这些测试钉住三件事：

1. 全是功能字的二元组被丢弃，实词与英文数字词必须保留（不能过滤过头）；
2. 同一条问题去掉外壳后覆盖率**只升不降**（过滤的目的就是这个）；
3. 极端输入（整句都是功能字）不能除零崩溃。
"""

from __future__ import annotations

from app.config import Settings
from app.services.answerability import FUNCTION_CHARS, information_terms, measure_coverage


def make_settings(**overrides) -> Settings:
    base = {
        "llm_provider": "stub",
        "answerability_min_coverage": 0.2,
        "answerability_high_score": 0.26,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_function_chars_are_the_expected_kind() -> None:
    """功能字表本身要"温和"：不能把 上/下/中/留/存 这类能构成实词的字放进去（留/存/竞 不在表里）。"""
    for char in "的了是我你他帮":
        assert char in FUNCTION_CHARS
    for char in "留存竞品路线崩溃":
        assert char not in FUNCTION_CHARS


def test_information_terms_drops_question_shell() -> None:
    """口语外壳（全功能字二元组）被丢弃，实词保留。"""
    terms = information_terms("8 月客服工单里，用户抱怨最多的是哪几类问题？帮我归一下类。")
    # 丢弃的问句外壳
    for shell in ("哪几", "多的", "帮我"):
        assert shell not in terms, f"{shell} 属于问句外壳，应被丢弃"
    # 必须保留的信息词
    for keep in ("客服", "工单", "用户", "抱怨", "问题"):
        assert keep in terms, f"{keep} 是信息词，不能被丢弃"


def test_information_terms_keeps_latin_and_numbers() -> None:
    """英文与数字词不受影响（它们不是中文二元组）。"""
    terms = information_terms("ARPU 和 NPS 分别是多少？8 月数据")
    assert "arpu" in terms and "nps" in terms
    assert "多少" not in terms  # 这是疑问词，属于外壳


def test_filtering_only_raises_coverage() -> None:
    """同一条问题，去掉外壳后覆盖率只升不降——这正是修复「误拒」的机理。"""
    from app.rag.retrieval import tokenize

    query = "8 月客服工单里，用户抱怨最多的是哪几类问题？帮我归一下类。"
    snippet = "客服工单分类：用户抱怨集中在闪退、卡顿、登录失败三类。"
    settings = make_settings()
    filtered = measure_coverage(query, [snippet], top_score=0.1, settings=settings)

    naive_terms = [t for t in {*tokenize(query)} if len(t) >= 2]
    naive_matched = [t for t in naive_terms if t in snippet.lower()]
    naive = len(naive_matched) / len(naive_terms)

    assert filtered.coverage > naive, "过滤后覆盖率必须高于不过滤"
    assert filtered.coverage >= 0.3
    assert abs(filtered.coverage - filtered.matched / filtered.terms) < 1e-9


def test_filler_heavy_question_is_answerable() -> None:
    """回归：这条真实用例（eval single_kb）曾因外壳稀释覆盖率被判"不可答"而误拒。"""
    query = "8 月客服工单里，用户抱怨最多的是哪几类问题？帮我归一下类。"
    snippets = [
        "客服工单月报：用户抱怨最多的三类问题是闪退、登录失败与消息延迟。",
        "工单归因规则：客服按抱怨类型打标，用户与产品经理均可查看。",
    ]
    coverage = measure_coverage(query, snippets, top_score=0.18, settings=make_settings())
    assert coverage.answerable is True
    assert coverage.coverage >= 0.2


def test_dropped_terms_are_reported() -> None:
    """被丢弃的词要留痕：改口径的影响必须能在报告/问答响应里复核。"""
    coverage = measure_coverage(
        "8 月客服工单里，用户抱怨最多的是哪几类问题？帮我归一下类。",
        ["客服工单分类：用户抱怨集中在闪退、卡顿、登录失败三类。"],
        settings=make_settings(),
    )
    payload = coverage.as_dict()
    assert payload["dropped_terms"], "应记录被丢弃的问句外壳"
    assert "帮我" in payload["dropped_terms"]
    # 覆盖率必须与"命中的信息词 / 信息词总数"一致（口径要能被复核）
    assert payload["coverage"] == round(payload["matched"] / payload["terms"], 4)


def test_all_function_query_does_not_crash() -> None:
    """整句都是功能字：词表为空 → 覆盖率 0，不得除零崩溃。"""
    coverage = measure_coverage("这是什么呢", ["随便什么材料"], settings=make_settings())
    assert coverage.terms == 0
    assert coverage.coverage == 0.0
    assert coverage.answerable is False


def test_high_score_escape_still_works() -> None:
    """过滤不能破坏"高分逃生口"：覆盖率低但相关度高时仍判可回答。"""
    coverage = measure_coverage(
        "这是为什么呢", ["材料"], top_score=0.9, settings=make_settings()
    )
    assert coverage.answerable is True

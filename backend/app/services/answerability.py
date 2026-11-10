"""可回答性判定的**共用**实现：问题关键信息在材料里覆盖了多少。

为什么单独抽出来：诊断工作流与「知识库问答」都必须用**同一套口径**判断
"这些材料够不够回答问题"。各写一份的后果是同一个问题在问答里说"能答"、
在诊断里说"证据不足"——用户会认为系统自相矛盾。

口径（与工作流节点保持一致）：
- `coverage` = 问题**信息词**（见下）在材料里出现的比例；
- `top_score` = 最高相关度（向量/混合检索分数）；
- **可回答** = coverage ≥ `answerability_min_coverage` 或 top_score ≥ `answerability_high_score`
  （后者是"高分逃生口"：覆盖率低但明显高度相关时仍然算可回答）。

## 为什么分母要过滤"问句外壳"

中文没有分词器，只能拿字符二元组当词（`tokenize`）。这会把问句的**口语外壳**也当成词：
「8 月客服工单里，用户抱怨最多的是哪几类问题？帮我归一下类。」会产生 `帮我`/`归一`/`哪几`/`多的`
这类二元组，它们在任何文档里都不可能出现，却和其他词一样占分母 —— 实测这条用例的覆盖率被压到
0.208，**低于一条真正无答案用例的 0.286**，于是"可回答的问题"被判成"拒答"。

规则：**全部由功能字组成的二元组一律丢弃**（`多少`/`一下`/`哪几`/`多的`/`这是`…）。
它是保守的 —— 实词几乎总含至少一个非功能字（`留存`/`竞品`/`路线图` 都保留），
只丢掉注定匹配不上的问句外壳。

依据（`scripts/sweep_coverage_rules.py` 用工作流实跑采集的完整命中词表离线扫描 24 条建模用例）：

| 口径 | 最优判定准确率 | 误拒 | 漏拒 | 可回答下限 / 应拒答上限 |
| --- | --- | --- | --- | --- |
| 不过滤（旧口径） | 0.958 | 1 | 0 | 0.208 / 0.286（重叠 0.078） |
| **丢"全功能字"二元组（本实现）** | 0.958 | **0** | 1 | 0.278 / 0.316（重叠 0.038） |
| 丢"含任一功能字"的二元组 | 0.958 | 0 | 1 | 0.385 / 0.400（重叠 0.015） |
| 再加 IDF 加权 | 0.875～0.917 | 0～1 | 0～3 | 更差 |

**要诚实看待这个改动的作用**：它**没有**提高判定准确率的上限（0.958 由两类用例的重叠决定，
见下），而是把重叠区从 0.078 收窄到 0.038 —— 于是"零误拒"的工作点整体上移，
可用的阈值区间从 `(0.21, 0.286)` 变成 `(0.28, 0.316)`，通过数因此多了 1 条。
更强的过滤（"含任一功能字"）重叠更小，但它把内容词也丢掉了（`下降`/`上线` 这类），风险更大；
本实现取保守规则：**只有整个二元组都由功能字组成才丢**。

已知局限（详见 README §8）：过滤后两类用例仍有重叠区（实测分离度 −0.015 ～ −0.038），
"问法被改写"与"库里根本没有这个主题"在词面数值上无法完全分开——跨过这个上限需要语义信号
（真实模型复核已接在 `combine_answerability` 里，或换语义 embedding）。阈值需要随之重新标定。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings

# 功能字：助词、代词、口语指令词、量词。全由这些字组成的二元组不可能是实词。
FUNCTION_CHARS = frozenset(
    "的了是和与在有对把被给就都而及或这那哪么什怎呢吗吧啊"
    "我你他她它们咱"
    "会能要可需应该想让请帮"
    "很更太挺"
    "个些次种条份点一二两几多少"
    "下上里中"
)


def information_terms(query: str) -> list[str]:
    """问题的**信息词**：去掉问句外壳（全功能字二元组）后的词表。

    只影响覆盖率的分母/分子，不影响检索打分（检索另有一套关键词权重）。
    """
    from app.rag.retrieval import tokenize

    terms = {term for term in tokenize(query or "") if len(term) >= 2}
    return sorted(term for term in terms if not set(term) <= FUNCTION_CHARS)


@dataclass
class Coverage:
    coverage: float
    threshold: float
    top_score: float
    high_score_escape: float
    terms: int
    matched: int
    missing_terms: list[str]
    answerable: bool
    dropped_terms: list[str] | None = None

    def as_dict(self) -> dict:
        return {
            "coverage": round(self.coverage, 4),
            "threshold": self.threshold,
            "top_score": round(self.top_score, 4),
            "high_score_escape": self.high_score_escape,
            "terms": self.terms,
            "matched": self.matched,
            "missing_terms": self.missing_terms,
            # 被当作"问句外壳"丢掉的词：留痕便于复核口径改动的影响
            "dropped_terms": self.dropped_terms or [],
            "answerable": self.answerable,
        }


def measure_coverage(
    query: str,
    snippets: list[str],
    *,
    top_score: float = 0.0,
    settings: Settings | None = None,
) -> Coverage:
    """计算覆盖率与可回答性。`snippets` 是材料文本（证据/检索切片）。"""
    from app.rag.retrieval import tokenize

    settings = settings or get_settings()
    all_terms = {term for term in tokenize(query or "") if len(term) >= 2}
    terms = information_terms(query)
    dropped = sorted(all_terms - set(terms))
    blob = " ".join((item or "").lower() for item in snippets)
    matched = [term for term in terms if term in blob]
    coverage = (len(matched) / len(terms)) if terms else 0.0
    threshold = settings.answerability_min_coverage
    escape = settings.answerability_high_score
    return Coverage(
        coverage=coverage,
        threshold=threshold,
        top_score=top_score,
        high_score_escape=escape,
        terms=len(terms),
        matched=len(matched),
        missing_terms=[term for term in terms if term not in blob][:12],
        answerable=coverage >= threshold or top_score >= escape,
        dropped_terms=dropped[:12],
    )


__all__ = ["Coverage", "FUNCTION_CHARS", "information_terms", "measure_coverage"]

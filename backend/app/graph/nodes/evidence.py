"""证据层节点：合并 → 校验充分性 → 补检索 / 冲突处理。

这一层是"工作流优先"的核心：
- `merge_evidence` 把各 Agent 的候选证据统一编号（E1、E2…），把结论里的局部引用
  重映射成全局引用，并做跨来源冲突检测；
- `verify_evidence` 用规则判定证据是否充分、风险等级，并决定下一条边；
- `retrieve_more` 是**有界**的补充检索（最多 2 轮），且新证据同样要重新合并且被引用；
- `resolve_conflict` 不合并冲突，只按来源优先级给出"采用哪一侧 + 为什么"，并转人工。
"""

from __future__ import annotations

import json
import re

from app.config import get_settings
from app.graph.runtime import NodeRuntime
from app.graph.state import AnalysisState, bump_visits
from app.logging_utils import get_logger
from app.tools.registry import ToolContext

logger = get_logger(__name__)

# 来源优先级：数值越小越可信（设计文档 10 节"说明采用的来源优先级"）
SOURCE_PRIORITY: dict[str, int] = {
    "metric": 0,
    "engineering_kb": 1,
    "product_kb": 2,
    "operation_kb": 3,
    "feedback_kb": 4,
    "document": 5,
}
PRIORITY_RULE_TEXT = "指标数据 > 研发监控/发布记录 > 产品文档 > 运营口径 > 用户反馈"

# 冲突检测的主题别名（中文 / 英文 / 指标名都能对上）
TOPIC_ALIASES: dict[str, tuple[str, ...]] = {
    "崩溃与闪退": ("崩溃", "闪退", "crash", "无响应", "anr"),
    "登录与账号": ("登录", "登陆", "login", "验证码", "账号"),
    "留存": ("留存", "retention", "回访"),
    "性能体验": ("卡顿", "耗时", "启动", "加载", "性能", "帧率"),
    "支付与转化": ("支付", "订单", "转化", "conversion", "gmv"),
}
NEGATION_MARKERS = (
    "未发现", "没有发现", "未上升", "无上升", "未出现", "未见", "无异常", "正常",
    "未受影响", "未恶化", "不存在", "无变化", "平稳", "未观测到", "无明显",
)
ASSERTION_MARKERS = (
    "上升", "增加", "集中", "下降", "失败", "闪退", "崩溃", "异常", "恶化", "下滑",
    "变差", "怀疑", "指向", "降至", "降幅", "升高", "走高", "走低", "高于", "低于",
)
# 形如 +63.6% / -16.4% 的定量变化同样构成"断言"
DELTA_PATTERN = r"[+\-]\s?\d+(?:\.\d+)?\s?%"
NEGATION_WINDOW = 12
# 冲突主题的优先级：故障类主题比"留存"这类结果指标更值得人工先看
TOPIC_PRIORITY = ("崩溃与闪退", "登录与账号", "性能体验", "留存", "支付与转化")
# 规则/验收类陈述不是"观测结论"，不能用来和事实打架
POLICY_MARKERS = (
    "若", "如果", "触发", "验收", "阈值", "目标值", "必须", "应当", "口径为",
    "负责", "评审通过", "排期", "观测窗口",
)


# --------------------------------------------------------------------------- 合并
def merge_evidence(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    settings = get_settings()
    with runtime.node_session(state, "merge_evidence", load_task=False) as ctx:
        raw = list(state.get("agent_evidence") or [])
        filtered = [item for item in raw if _keep_evidence(item, settings)]

        evidence: list[dict] = []
        seen: set[str] = set()
        id_map: dict[tuple[str, str], str] = {}
        for item in filtered:
            key = _dedupe_key(item)
            if key in seen:
                continue
            seen.add(key)
            evidence_id = f"E{len(evidence) + 1}"
            record = dict(item)
            record["evidence_id"] = evidence_id
            record.pop("rows", None)  # 行数据留在 metric_results，证据里只保摘要与行数
            if item.get("source_kind") == "metric" and record.get("row_count") is None:
                record["row_count"] = len(item.get("rows") or [])
            evidence.append(record)
            if item.get("local_ref"):
                id_map[(item.get("agent_code") or "", item["local_ref"])] = evidence_id

        statements = _remap_statements(state, id_map)
        conflicts = detect_conflicts(statements, evidence)
        coverage = _build_coverage(state, evidence, statements, conflicts, settings)

        ctx.emit(
            f"证据合并完成：{len(evidence)} 条证据（原始 {len(raw)} 条），"
            f"{len(statements)} 条候选陈述，{len(conflicts)} 处冲突",
            {
                "dropped": len(raw) - len(evidence),
                "conflicts": [c["topic"] for c in conflicts],
                "metric_available": coverage.get("metric_available"),
            },
            level="warn" if conflicts else "info",
        )
        return {
            "evidence": evidence,
            "metric_results": [
                {**item} for item in (state.get("agent_metric_results") or []) if item.get("local_ref")
            ],
            "statements": statements,
            "conflicts": conflicts,
            "coverage": coverage,
            "node_visits": bump_visits(state, "merge_evidence"),
        }


def _keep_evidence(item: dict, settings) -> bool:
    if item.get("permission_checked") is False:
        return False  # 未通过权限校验的证据一律不得进入报告
    if item.get("source_kind") == "metric":
        return True
    score = item.get("relevance_score")
    if score is None:
        return True
    return float(score) >= settings.evidence_min_relevance


def _dedupe_key(item: dict) -> str:
    if item.get("source_kind") == "metric":
        params = json.dumps(item.get("params") or {}, sort_keys=True, ensure_ascii=False)
        return f"metric|{item.get('metric_name')}|{item.get('capability')}|{params}"
    return f"doc|{item.get('chunk_id') or item.get('document_id')}"


def _remap_statements(state: AnalysisState, id_map: dict[tuple[str, str], str]) -> list[dict]:
    """把各 Agent 结论中的局部证据引用重映射为全局证据编号。"""
    statements: list[dict] = []
    for agent_code, payload in (state.get("agent_results") or {}).items():
        findings = list(payload.get("findings") or []) + list(payload.get("supplemental_findings") or [])
        for finding in findings:
            refs = []
            for local in finding.get("evidence_refs") or []:
                global_ref = id_map.get((agent_code, local))
                if global_ref:
                    refs.append(global_ref)
            statements.append(
                {
                    "id": finding.get("id") or f"{agent_code}-{len(statements) + 1}",
                    "type": finding.get("type") or "fact",
                    "text": finding.get("text") or "",
                    "evidence_refs": refs,
                    "confidence": finding.get("confidence", 0.6),
                    "source_agent": agent_code,
                    "tags": finding.get("tags") or [],
                    "supplemental": bool(finding.get("supplemental")),
                }
            )
    return statements


# --------------------------------------------------------------------------- 冲突
MAX_CONFLICTS = 8


def detect_conflicts(statements: list[dict], evidence: list[dict]) -> list[dict]:
    """跨来源冲突检测：同一主题下，一方断言存在、另一方否认时判定为冲突。

    刻意做得"保守"：只有**事实类陈述**、**跨 Agent（跨来源）**、**主题一致且极性相反**
    才算冲突，并按主题+来源对去重、总量封顶。宁可漏报，也不要把普通表述差异
    堆成上百条"冲突"而稀释真正需要人工裁决的那几条。

    只检测、不裁决——裁决在 `resolve_conflict`，且最终必须人工确认。
    """
    evidence_by_id = {item["evidence_id"]: item for item in evidence}
    candidates: list[dict] = []
    seen_pairs: set[tuple] = set()

    facts = [
        s
        for s in statements
        if s.get("type") == "fact"
        and not s.get("supplemental")
        and not _is_policy_statement(s.get("text") or "")
    ]
    for index, left in enumerate(facts):
        left_topics = _topics_of(left["text"])
        if not left_topics:
            continue
        left_polarity = _topic_polarity(left["text"], left_topics)
        for right in facts[index + 1 :]:
            if right.get("source_agent") == left.get("source_agent"):
                continue
            shared = left_topics & _topics_of(right["text"])
            if not shared:
                continue
            right_polarity = _topic_polarity(right["text"], shared)
            # 极性必须挂在**同一个主题**上：避免"某指标无异常点"被误判为"否认留存下降"
            negated = sorted(t for t in shared if left_polarity.get(t) == "negation") or sorted(
                t for t in shared if right_polarity.get(t) == "negation"
            )
            asserted = sorted(t for t in shared if left_polarity.get(t) == "assertion") or sorted(
                t for t in shared if right_polarity.get(t) == "assertion"
            )
            common = sorted(set(negated) & set(asserted))
            if not common:
                continue
            left_docs = _documents_of(left, evidence_by_id)
            right_docs = _documents_of(right, evidence_by_id)
            if left_docs & right_docs:
                continue  # 同一份文档内部的两句话不算"跨来源冲突"
            left_kinds = _source_kinds_of(left, evidence_by_id)
            right_kinds = _source_kinds_of(right, evidence_by_id)
            if left_kinds == right_kinds and "metric" not in left_kinds:
                continue  # 同类型来源（同为普通文档）且无指标参与时不做冲突判定
            topic = _pick_topic(set(common), left["text"], right["text"])
            pair_key = (topic, tuple(sorted({left.get("source_agent"), right.get("source_agent")})))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            refs = sorted({*(left.get("evidence_refs") or []), *(right.get("evidence_refs") or [])})
            candidates.append(
                {
                    "id": f"conflict-{len(candidates) + 1}",
                    "topic": topic,
                    "statement_a": {
                        "id": left["id"],
                        "text": left["text"],
                        "agent_code": left.get("source_agent"),
                        "evidence_refs": left.get("evidence_refs") or [],
                    },
                    "statement_b": {
                        "id": right["id"],
                        "text": right["text"],
                        "agent_code": right.get("source_agent"),
                        "evidence_refs": right.get("evidence_refs") or [],
                    },
                    "evidence_refs": refs,
                    "sources": [
                        evidence_by_id[ref].get("source_name")
                        for ref in refs
                        if ref in evidence_by_id
                    ][:4],
                    "source_kinds": sorted(left_kinds | right_kinds),
                    "detected_by": [left.get("source_agent"), right.get("source_agent")],
                    "resolution": None,
                }
            )

    # 指标与文档之间的冲突优先呈现（信息量最高）
    candidates.sort(key=lambda c: 0 if "metric" in (c.get("source_kinds") or []) else 1)
    return candidates[:MAX_CONFLICTS]


def _is_policy_statement(text: str) -> bool:
    """规则、验收标准、排期这类陈述描述"应该怎样"，不能作为冲突的一方。"""
    return any(marker in text for marker in POLICY_MARKERS)


def _documents_of(statement: dict, evidence_by_id: dict[str, dict]) -> set[str]:
    return {
        evidence_by_id[ref].get("document_id")
        for ref in statement.get("evidence_refs") or []
        if ref in evidence_by_id and evidence_by_id[ref].get("document_id")
    }


def _source_kinds_of(statement: dict, evidence_by_id: dict[str, dict]) -> set[str]:
    kinds = set()
    for ref in statement.get("evidence_refs") or []:
        item = evidence_by_id.get(ref)
        if not item:
            continue
        if item.get("source_kind") == "metric":
            kinds.add("metric")
        else:
            kinds.add(item.get("knowledge_base") or "document")
    return kinds


def _topics_of(text: str) -> set[str]:
    lowered = (text or "").lower()
    return {
        topic
        for topic, aliases in TOPIC_ALIASES.items()
        if any(alias.lower() in lowered for alias in aliases)
    }


def _pick_topic(shared: set[str], left_text: str, right_text: str) -> str:
    """在共同主题中挑一个最能代表冲突的：先看故障类优先级，再看命中次数。"""

    def score(topic: str) -> tuple[int, int, int]:
        priority = TOPIC_PRIORITY.index(topic) if topic in TOPIC_PRIORITY else len(TOPIC_PRIORITY)
        aliases = tuple(alias.lower() for alias in TOPIC_ALIASES.get(topic, ()))
        hits = sum(
            (text or "").lower().count(alias)
            for text in (left_text, right_text)
            for alias in aliases
        )
        return (priority, -hits, len(topic))

    return sorted(shared, key=score)[0]


def _polarity(text: str) -> str:
    """整句极性（仅保留给调用方参考，冲突判定请用 `_topic_polarity`）。"""
    lowered = (text or "").lower()
    if any(marker in lowered for marker in NEGATION_MARKERS):
        return "negation"
    if re.search(DELTA_PATTERN, lowered) or any(m in lowered for m in ASSERTION_MARKERS):
        return "assertion"
    return "neutral"


def _topic_polarity(text: str, topics: set[str]) -> dict[str, str]:
    """逐主题判定极性：否定词必须出现在该主题词附近，才算"否认该主题"。

    这条约束很关键："异常检测未发现显著异常点"里出现的"未发现"离"留存"很远，
    因此不会被误判成"否认留存下降"。
    """
    lowered = (text or "").lower()
    has_delta = bool(re.search(DELTA_PATTERN, lowered))
    has_assertion = any(m in lowered for m in ASSERTION_MARKERS)
    verdict: dict[str, str] = {}
    for topic in topics:
        negated = False
        for alias in TOPIC_ALIASES.get(topic, ()):
            keyword = alias.lower()
            start = 0
            while True:
                position = lowered.find(keyword, start)
                if position == -1:
                    break
                window = lowered[max(0, position - NEGATION_WINDOW) : position]
                if any(marker in window for marker in NEGATION_MARKERS):
                    negated = True
                    break
                start = position + len(keyword)
            if negated:
                break
        if negated:
            verdict[topic] = "negation"
        elif has_delta or has_assertion:
            verdict[topic] = "assertion"
        else:
            verdict[topic] = "neutral"
    return verdict


# --------------------------------------------------------------------------- 充分性
def _model_answerability(state: AnalysisState, evidence: list[dict]) -> dict | None:
    """可选的模型复核：模型不可用或输出不合法时返回 None，由词面规则兜底。"""
    from app.services.model_gateway import get_model_gateway

    gateway = get_model_gateway()
    profile = gateway.profile_for("answerability_check", sample_key=state.get("task_id"))
    if not profile.configured or not evidence:
        return None
    digest = "\n".join(
        f"[{item.get('evidence_id')}] {(item.get('content') or '')[:240]}"
        for item in evidence[:12]
    )
    payload, usage = gateway.chat_json(
        "你是证据充分性审查员。判断给定证据能否支撑回答该问题。"
        '只输出 JSON：{"answerable": true/false, "reason": "一句话依据"}。'
        "证据足以覆盖问题的关键点、或能明确给出否定性结论（例如'数据里没有该主题'）时返回 true；"
        "只有当证据与该问题确实无关、缺少回答所必需的关键事实时才返回 false。"
        "不要因为措辞改写或术语不同就判 false。",
        f"<问题>{state.get('query')}</问题>\n<证据>\n{digest}\n</证据>",
        fallback=lambda: {},
        max_tokens=200,
        profile=profile,
    )
    if usage.stub or not isinstance(payload, dict) or "answerable" not in payload:
        return None
    return {
        "answerable": bool(payload.get("answerable")),
        "reason": str(payload.get("reason") or "")[:300],
        "model": usage.model,
    }


def combine_answerability(
    rule: dict, model_verdict: dict | None, settings
) -> str:
    """合并"规则判定"与"模型复核"，就地更新 `rule` 并返回最终裁决方（rule / model）。

    分工的依据是**各自可靠的那一段**：

    - 规则强（覆盖率或最高相关度达到高分档）→ 规则说了算。证据充分时不接受模型推翻；
    - 规则弱或含糊 → 模型说了算。这正是"问法被改写"与"库里没有这个主题"重叠的区域，
      也是纯词面指标最不可靠的地方。

    抽成独立函数是为了可测：这段决策直接影响"报告有没有事实结论"，必须有单测钉住。
    """
    rule_answerable = bool(rule.get("answerable"))
    high = settings.answerability_high_score
    strong_rule = float(rule.get("coverage") or 0) >= high or float(rule.get("top_score") or 0) >= high
    rule["rule_answerable"] = rule_answerable
    if model_verdict is None:
        rule["decided_by"] = "rule"
        return "rule"
    rule["model_verdict"] = model_verdict
    if rule_answerable and strong_rule:
        rule["decided_by"] = "rule"
        rule["model_overridden"] = False
        return "rule"
    rule["decided_by"] = "model"
    rule["model_overridden"] = True
    rule["answerable"] = bool(model_verdict.get("answerable"))
    if not rule["answerable"]:
        rule["reason"] = model_verdict.get("reason")
    return "model"


def _build_coverage(
    state: AnalysisState,
    evidence: list[dict],
    statements: list[dict],
    conflicts: list[dict],
    settings,
) -> dict:
    doc_evidence = [e for e in evidence if e.get("source_kind") != "metric"]
    metric_evidence = [e for e in evidence if e.get("source_kind") == "metric"]
    metric_results = list(state.get("agent_metric_results") or [])
    # 行数据不在 evidence 里（避免 state 膨胀），用 row_count / metric_results 判断可用性
    metric_available = any(int(item.get("row_count") or 0) > 0 for item in metric_evidence) or any(
        item.get("rows") for item in metric_results
    )
    facts = [s for s in statements if s["type"] == "fact"]

    gaps: list[str] = []
    for payload in (state.get("agent_results") or {}).values():
        gaps.extend(payload.get("data_gaps") or [])

    quality_flags = sorted(
        {flag for item in metric_evidence for flag in (item.get("quality_flags") or [])}
    )
    warnings = [w for item in metric_evidence for w in (item.get("warnings") or [])]

    answerability = _answerability(state.get("query") or "", evidence, settings)
    # 配置了真实模型时，用一次受控的语义判断复核"证据能不能回答这个问题"：
    # 纯词面覆盖率无法区分"问法被改写"与"库里根本没有这个主题"，
    # 而这恰恰是大模型最擅长、且不涉及取数权限的判断。
    #
    # 但**模型不能推翻"规则已经明确可回答"的结论**：模型只看到前若干条证据摘要
    # （证据多时看不到全貌），一旦让它否掉充分证据，就会把大量正常问题误判成"无答案"，
    # 报告随之丢弃全部事实结论。实测：任其覆盖规则判定时，真实模型下 50 条评测里
    # single_kb/cross_kb 类通过率从 0.9 掉到 0.2（46 条证据、26 条事实却给出"证据不足"）。
    # 因此这里的分工是：规则强（覆盖率或相关度达到高分档）→ 直接可回答；
    # 规则弱/含糊 → 由模型裁决（这才是模型真正擅长的那一段）。
    model_verdict = _model_answerability(state, evidence)
    decided_by = combine_answerability(answerability, model_verdict, settings)
    if not answerability["answerable"] and evidence:
        # 文案必须与"谁做的判定"一致：模型裁决时再打印覆盖率会自相矛盾（例如 40% < 20%）
        if decided_by == "model":
            gaps.append(
                "模型复核认为现有证据不足以回答该问题"
                + (f"（{answerability.get('reason')}）" if answerability.get("reason") else "")
            )
        else:
            gaps.append(
                f"检索到的材料与问题的关键信息匹配度低（覆盖率 "
                f"{answerability['coverage']:.0%} < {answerability['threshold']:.0%}）"
            )

    filters = state.get("filters") or {}
    target_version = filters.get("app_version")
    alignment = {"target_version": target_version, "matched": None, "note": None}
    if target_version:
        version_hits = [
            item
            for item in metric_evidence
            if (item.get("params") or {}).get("app_version") == target_version
        ]
        with_rows = [item for item in version_hits if int(item.get("row_count") or 0) > 0]
        alignment["matched"] = bool(with_rows)
        alignment["queries"] = len(version_hits)
        if version_hits and not with_rows:
            alignment["note"] = f"版本 {target_version} 的指标查询没有返回任何数据行"
            gaps.append(alignment["note"])
        elif not version_hits:
            alignment["matched"] = False
            alignment["note"] = f"没有针对版本 {target_version} 的指标查询"
            gaps.append(alignment["note"])
    return {
        "evidence_count": len(evidence),
        "document_evidence_count": len(doc_evidence),
        "metric_evidence_count": len(metric_evidence),
        "fact_count": len(facts),
        "inference_count": len([s for s in statements if s["type"] == "inference"]),
        "hypothesis_count": len([s for s in statements if s["type"] == "hypothesis"]),
        "conflict_count": len(conflicts),
        "metric_available": metric_available,
        "metric_quality_flags": quality_flags,
        "metric_warnings": warnings,
        "data_gaps": list(dict.fromkeys(gaps))[:12],
        "min_evidence_required": settings.min_evidence_count,
        "version_alignment": alignment,
        "answerability": answerability,
        "agents": {
            code: {
                "ok": payload.get("ok"),
                "findings": len(payload.get("findings") or []),
                "confidence": payload.get("confidence"),
                "skipped": bool(payload.get("skipped")),
            }
            for code, payload in (state.get("agent_results") or {}).items()
        },
    }


def _answerability(query: str, evidence: list[dict], settings) -> dict:
    """可回答性判断：问题的关键信息在证据里到底覆盖了多少。

    实现在 `services/answerability.py`（**共用**）：诊断工作流与「知识库问答」
    必须用同一套口径，否则同一个问题会出现"问答说能答、诊断说证据不足"的自相矛盾。
    这里只负责把证据列表转成文本 + 取最高相关度（指标类证据不参与文本覆盖率）。
    """
    from app.services.answerability import measure_coverage

    doc_evidence = [item for item in evidence if item.get("source_kind") != "metric"]
    top_score = max(
        (float(item.get("relevance_score") or 0) for item in doc_evidence),
        default=0.0,
    )
    coverage = measure_coverage(
        query,
        [item.get("content") or "" for item in evidence],
        top_score=top_score,
        settings=settings,
    )
    return coverage.as_dict()


# --------------------------------------------------------------------------- 判定
def verify_evidence(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    settings = runtime.settings
    coverage = state.get("coverage") or {}
    conflicts = state.get("conflicts") or []
    retrieval_rounds = int(state.get("retrieval_rounds") or 0)
    conflict_rounds = int(state.get("conflict_rounds") or 0)
    unresolved = [c for c in conflicts if not c.get("resolution")]

    evidence_count = int(coverage.get("evidence_count") or 0)
    fact_count = int(coverage.get("fact_count") or 0)
    metric_available = bool(coverage.get("metric_available"))
    injection = state.get("injection") or {}

    reasons: list[str] = []
    sufficient = True
    if evidence_count < settings.min_evidence_count:
        sufficient = False
        reasons.append(f"证据条数不足（{evidence_count} < {settings.min_evidence_count}）")
    if fact_count == 0:
        sufficient = False
        reasons.append("没有任何可引用的事实性结论")
    if coverage.get("answerability", {}).get("answerable") is False:
        sufficient = False
        answer = coverage["answerability"]
        reasons.append(
            f"问题关键信息在证据中覆盖不足（{answer['coverage']:.0%} < {answer['threshold']:.0%}），"
            "现有材料不足以回答该问题"
        )
    if not metric_available:
        reasons.append("缺少指标数据支撑，结论只能停留在文本证据层面")
    if coverage.get("version_alignment", {}).get("matched") is False:
        sufficient = False
        reasons.append("版本与数据时间不匹配，无法给出该版本的指标结论")

    confidence = _average_confidence(state)
    risk_level, needs_review, risk_reasons = _policy().assess_risk(
        confidence=confidence,
        evidence_count=evidence_count,
        conflict_count=len(conflicts),
        injection_hit=bool(injection),
        metric_available=metric_available,
        priority=(state.get("filters") or {}).get("priority"),
    )
    if state.get("force_human_review"):
        needs_review = True
        risk_reasons.append("任务被显式要求人工审核")
    if conflicts:
        # 冲突一旦出现就必须人工确认（即使已经按来源优先级裁决过）
        needs_review = True
        risk_reasons.append(f"存在 {len(conflicts)} 处跨来源证据冲突，需人工确认")
    if state.get("needs_human_review"):
        needs_review = True
    if not sufficient:
        risk_reasons.extend(reasons)

    if not sufficient and retrieval_rounds < settings.workflow_max_retrieve_rounds:
        branch = "retrieve_more"
    elif unresolved and conflict_rounds == 0:
        branch = "resolve_conflict"
    elif needs_review:
        branch = "human_approval"
    else:
        branch = "generate_report"

    sufficiency = {
        "sufficient": sufficient,
        "reasons": reasons,
        "evidence_count": evidence_count,
        "fact_count": fact_count,
        "metric_available": metric_available,
        "confidence": confidence,
        "retrieval_rounds": retrieval_rounds,
        "max_retrieval_rounds": settings.workflow_max_retrieve_rounds,
    }
    verification = {
        "branch": branch,
        "unresolved_conflicts": len(unresolved),
        "conflict_rounds": conflict_rounds,
        "needs_human_review": needs_review,
        "reasons": risk_reasons,
    }

    with runtime.node_session(state, "verify_evidence", load_task=False) as ctx:
        ctx.emit(
            f"证据校验：充分性={'通过' if sufficient else '不足'}，风险={risk_level}，"
            f"下一步={branch}",
            {
                "sufficiency": sufficiency,
                "risk_reasons": risk_reasons,
                "unresolved_conflicts": len(unresolved),
            },
            level="warn" if branch != "generate_report" else "info",
        )
        return {
            "sufficiency": sufficiency,
            "verification": verification,
            "risk_level": risk_level,
            "risk_reasons": risk_reasons,
            "needs_human_review": needs_review,
            "branch": branch,
            "node_visits": bump_visits(state, "verify_evidence"),
        }


def route_after_verify(state: AnalysisState) -> str:
    return state.get("branch") or "generate_report"


def _average_confidence(state: AnalysisState) -> float:
    values = [
        float(payload.get("confidence") or 0)
        for payload in (state.get("agent_results") or {}).values()
        if payload.get("ok")
    ]
    return round(sum(values) / len(values), 4) if values else 0.0


def _policy():
    from app.services.policy_engine import get_policy_engine

    return get_policy_engine()


# --------------------------------------------------------------------------- 补检索
RELAXED_CAPABILITY_RETRY = ("get_trend", "breakdown", "detect_anomaly")


def retrieve_more(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """有界补充检索：扩大召回 + 放宽指标过滤条件。

    只增加**证据**，不伪造结论：新证据重新进入 `merge_evidence` 后照样参与
    冲突检测，并可被报告引用。是否"因为补检索而变得充分"由证据条数与
    事实条数共同决定，而不是靠降低标准。
    """
    from app.tools.registry import get_tool_registry

    registry = get_tool_registry()
    coverage = state.get("coverage") or {}
    gaps = coverage.get("data_gaps") or []
    round_no = int(state.get("retrieval_rounds") or 0) + 1

    new_evidence: list[dict] = []
    new_metrics: list[dict] = []
    warnings: list[str] = []

    # short_writes=True：本节点会调用工具（写 tool_calls 审计），必须走短事务
    with runtime.node_session(state, "retrieve_more", load_task=False, short_writes=True) as ctx:
        ctx.emit(f"第 {round_no} 轮补充检索：目标缺口 {len(gaps)} 项", {"gaps": gaps[:5]})

        for plan_item in state.get("plan") or []:
            agent_code = plan_item.get("agent")
            if agent_code == "report_agent":
                continue
            kb_codes = set(plan_item.get("knowledge_bases") or [])
            tool_ctx = ToolContext(
                session=ctx.session,
                task_id=state.get("task_id", ""),
                tenant_id=state.get("tenant_id", "*"),
                user_id=state.get("user_id", ""),
                agent_code=agent_code,
                max_security_rank=int(state.get("max_security_rank") or 1),
                allowed_kb_codes=kb_codes,
                budget=runtime.settings.agent_max_tool_calls,
            )

            if kb_codes:
                # 用缺口描述改写检索问法，并放宽版本过滤，换一批切片
                query = state.get("query", "")
                if gaps:
                    query = f"{query} {' '.join(str(g) for g in gaps[:3])}"
                outcome = registry.call(
                    "retrieval.search",
                    {
                        "query": query,
                        "knowledge_bases": sorted(kb_codes),
                        "top_k": runtime.settings.retrieval_top_k,
                    },
                    tool_ctx,
                )
                if outcome.ok:
                    start = _next_local_index(state, new_evidence, agent_code, "E")
                    for offset, hit in enumerate(outcome.data.get("hits") or []):
                        item = dict(hit)
                        item["local_ref"] = f"E{start + offset}"
                        item["agent_code"] = agent_code
                        item["source_kind"] = "document"
                        item["supplemental_round"] = round_no
                        new_evidence.append(item)
                else:
                    warnings.append(f"{agent_code} 补充检索失败：{outcome.error}")

            if agent_code == "metric_agent":
                # 放宽维度过滤后重试，覆盖"某个切片没数据"的缺口
                retry_specs = [
                    q
                    for q in (plan_item.get("metric_queries") or [])
                    if q.get("capability") in RELAXED_CAPABILITY_RETRY
                    and (q.get("params") or {}).get("dimension") != "device"
                ]
                for query_spec in retry_specs[:3]:
                    relaxed = dict(query_spec.get("params") or {})
                    relaxed.pop("device", None)
                    relaxed.pop("channel", None)
                    outcome = registry.call(
                        "metric.query",
                        {"capability": query_spec.get("capability"), "params": relaxed},
                        tool_ctx,
                    )
                    if not outcome.ok or not outcome.data.get("rows"):
                        continue
                    start = _next_local_index(state, new_evidence, agent_code, "M")
                    rows = outcome.data.get("rows") or []
                    item = {
                        "local_ref": f"M{start}",
                        "agent_code": agent_code,
                        "source_kind": "metric",
                        "metric_name": outcome.data.get("metric_name"),
                        "capability": outcome.data.get("capability"),
                        "params": outcome.data.get("params"),
                        "summary": outcome.data.get("summary"),
                        "row_count": len(rows),
                        "dataset_version": outcome.data.get("dataset_version"),
                        "source_version": outcome.data.get("source_version"),
                        "quality_flags": outcome.data.get("quality_flags") or [],
                        "warnings": outcome.data.get("warnings") or [],
                        "content": f"补充指标查询 {outcome.data.get('metric_name')}"
                        f"（{outcome.data.get('capability')}，放宽维度过滤）",
                        "source_name": f"指标服务:{outcome.data.get('metric_name')}",
                        "relevance_score": 1.0,
                        "permission_checked": True,
                        "supplemental_round": round_no,
                    }
                    new_evidence.append(item)
                    new_metrics.append(item)

        ctx.emit(
            f"补充检索完成：新增 {len(new_evidence)} 条证据（其中指标 {len(new_metrics)} 条）",
            {"round": round_no, "warnings": warnings},
            level="warn" if warnings else "info",
        )
        return {
            "agent_evidence": new_evidence,
            "agent_metric_results": new_metrics,
            "retrieval_rounds": 1,
            "warnings": warnings,
            "node_visits": bump_visits(state, "retrieve_more"),
        }


def _next_local_index(
    state: AnalysisState, new_evidence: list[dict], agent_code: str, prefix: str
) -> int:
    counters = [
        _local_index(item.get("local_ref"), prefix)
        for item in (state.get("agent_evidence") or []) + new_evidence
        if item.get("agent_code") == agent_code
    ]
    return max(counters, default=0) + 1


def _local_index(ref: str | None, prefix: str) -> int:
    if not ref or not ref.startswith(prefix):
        return 0
    try:
        return int(ref[len(prefix) :])
    except ValueError:
        return 0


# --------------------------------------------------------------------------- 冲突处理
def resolve_conflict(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """按来源优先级给出采用结论，不删除任何一方，并强制人工审核。"""
    conflicts = state.get("conflicts") or []
    evidence_by_id = {item["evidence_id"]: item for item in (state.get("evidence") or [])}
    resolved: list[dict] = []

    for conflict in conflicts:
        if conflict.get("resolution"):
            resolved.append(conflict)
            continue
        side_a = _side_priority(conflict.get("statement_a") or {}, evidence_by_id)
        side_b = _side_priority(conflict.get("statement_b") or {}, evidence_by_id)
        adopted, rejected = (
            (conflict["statement_a"], conflict["statement_b"])
            if side_a[0] <= side_b[0]
            else (conflict["statement_b"], conflict["statement_a"])
        )
        adopted_priority = min(side_a[0], side_b[0])
        resolved.append(
            {
                **conflict,
                "resolution": {
                    "adopted_statement": adopted.get("id"),
                    "adopted_source": adopted.get("agent_code"),
                    "rejected_statement": rejected.get("id"),
                    "rejected_source": rejected.get("agent_code"),
                    "priority_rule": PRIORITY_RULE_TEXT,
                    "reason": f"按来源优先级，建议采用优先级 {adopted_priority} 的一侧"
                    f"（{adopted.get('agent_code')}）；该建议需人工确认，"
                    f"另一侧证据保留在报告中作为反证。",
                    "requires_human": True,
                },
            }
        )

    with runtime.node_session(state, "resolve_conflict", load_task=False) as ctx:
        ctx.emit(
            f"冲突处理完成：{len(resolved)} 处冲突已记录双方结论与来源优先级，转人工确认",
            {"topics": [c.get("topic") for c in resolved]},
            level="warn",
        )
        ctx.audit("conflict_resolved", detail={"conflict_ids": [c.get("id") for c in resolved]})
        return {
            "conflicts": resolved,
            "conflict_rounds": 1,
            "needs_human_review": True,
            "node_visits": bump_visits(state, "resolve_conflict"),
        }


def _side_priority(statement: dict, evidence_by_id: dict[str, dict]) -> tuple[int, str]:
    priorities = []
    for ref in statement.get("evidence_refs") or []:
        item = evidence_by_id.get(ref)
        if not item:
            continue
        if item.get("source_kind") == "metric":
            priorities.append(SOURCE_PRIORITY["metric"])
        else:
            priorities.append(SOURCE_PRIORITY.get(item.get("knowledge_base") or "", SOURCE_PRIORITY["document"]))
    return (min(priorities) if priorities else SOURCE_PRIORITY["document"], statement.get("id") or "")


__all__ = [
    "PRIORITY_RULE_TEXT",
    "SOURCE_PRIORITY",
    "detect_conflicts",
    "merge_evidence",
    "resolve_conflict",
    "retrieve_more",
    "route_after_verify",
    "verify_evidence",
]

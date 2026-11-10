"""知识库问答（检索即答）：让"问一句"和"跑一次完整诊断"各归其位。

为什么要有这一层：诊断任务链路的每一环（4 个 Agent 并行、冲突裁决、报告校验、人工审核）
都是为了让**结论可信**，但对"埋点字典里 user_login 的口径是什么"这类问题，
全套流程既慢又重。于是这里提供"先问一句"的入口：

- **检索即答**：跨可见知识库检索 → 给出答案要点 + **可定位引用**（文档名 / 页码 / 原文片段）；
- **不做无出处的总结**：答案要点来自检索到的切片原文，引用 id 一定存在（模型编造的引用会被丢弃）；
- **检索不到就直说**：命中不足时返回 `insufficient=true` 与"缺什么"，绝不硬答；
- **权限与密级和任务入口完全一致**：显式请求无权知识库 → 403；问题命中受限指标域 → 403；
  只检索当前用户可见且在其密级内的内容；
- **一键升级**：界面上可把同一个问题带到「新建诊断」，跑完整流程（冲突、多 Agent、人工审核）。

和诊断任务的分工（界面上也会提示）：

| | 知识库问答（本模块） | 诊断任务（新建诊断） |
|---|---|---|
| 耗时 | 秒级（纯检索，可选一次模型归纳） | 20 秒~2 分钟（4 Agent + 报告） |
| 输出 | 答案要点 + 引用片段 | 带优先级/影响范围/冲突裁决/不确定性的报告 |
| 冲突 | 不处理（只列出来源） | 保留双方并裁决 |
| 审计 | 记一条 `knowledge_qa` 审计 | 全链路事件、Agent 运行、工具调用留痕 |
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.logging_utils import get_logger
from app.rag.retrieval import RetrievalHit, RetrievalService
from app.services.metric_access import domain_denials

logger = get_logger(__name__)

# 答案要点最多给几条、每条多长（太长就不是"要点"了）
MAX_POINTS = 5
# 一条切片可能是一组完整规则或编号清单；320 字会把同一节的后半部分截掉。
# 600 字足以覆盖当前 600 字切片上限，同时仍由 MAX_POINTS 控制总体展示量。
POINT_MAX_CHARS = 600
SNIPPET_MAX_CHARS = 240
# 前端引用摘要需要短，但模型归纳必须看到更完整的切片；两者不能共用同一个截断长度。
# 以默认最多 6 条命中计算，最坏约 7,200 字符，仍在常见模型上下文的安全范围内。
MODEL_CONTEXT_MAX_CHARS = 1200
# 直接答案面向普通业务用户：超过这个长度通常已经退化成原文拼接。
DIRECT_ANSWER_MAX_CHARS = 280
DIRECT_ANSWER_MAX_CLAUSES = 4


@dataclass
class QaCitation:
    index: int
    chunk_id: str
    document_id: str
    kb_code: str
    source_name: str
    page: int | None
    document_version: str
    score: float
    snippet: str

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "kb_code": self.kb_code,
            "source_name": self.source_name,
            "page": self.page,
            "document_version": self.document_version,
            "score": round(self.score, 4),
            "snippet": self.snippet,
        }


@dataclass
class QaAnswer:
    query: str
    answer: str
    points: list[str] = field(default_factory=list)
    citations: list[QaCitation] = field(default_factory=list)
    insufficient: bool = False
    missing: list[str] = field(default_factory=list)
    mode: str = "extractive"
    knowledge_bases: list[str] = field(default_factory=list)
    latency_ms: int = 0
    model: str | None = None
    warnings: list[str] = field(default_factory=list)
    # 与诊断工作流同一套覆盖率口径的结果（便于前端展示"为什么说检索不到"）
    coverage: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        disclaimer = (
            "当前检索结果不足以直接回答这个问题；下方材料仅供核对，不能视为答案。"
            "建议补充相关资料、调整问法，或点「转成完整诊断」继续核实。"
            if self.insufficient
            else "这是检索即答：结论直接来自下方引用片段，未做冲突裁决与人工审核；需要严谨结论请点「转成完整诊断」。"
        )
        return {
            "query": self.query,
            "answer": self.answer,
            "points": self.points,
            "citations": [item.as_dict() for item in self.citations],
            "insufficient": self.insufficient,
            "missing": self.missing,
            "mode": self.mode,
            "knowledge_bases": self.knowledge_bases,
            "latency_ms": self.latency_ms,
            "model": self.model,
            "warnings": self.warnings,
            "coverage": self.coverage,
            "disclaimer": disclaimer,
        }


class QaPermissionError(Exception):
    """越权（知识库范围或指标密级）：由路由层转成 403。"""

    def __init__(self, message: str, *, code: str = "permission_denied") -> None:
        super().__init__(message)
        self.code = code


def _clean(text: str) -> str:
    """切片里常有换行/多余空白，压成一行便于在回答里展示。"""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _first_sentences(text: str, *, max_chars: int = POINT_MAX_CHARS) -> str:
    """取最有信息量的开头片段，避免把 Markdown 数字标题 ``2.`` 当成完整句子。"""
    cleaned = _clean(text)
    if len(cleaned) <= max_chars:
        return cleaned
    # ASCII 句点常出现在 Markdown 数字章节（"2. 核心事件"）和版本号中，
    # 不能作为通用断句符；否则长表格切片会只剩一个毫无信息量的 "2."。
    pieces = re.split(r"(?<=[。；;！？!?])\s*", cleaned)
    out = ""
    for piece in pieces:
        if not piece:
            continue
        if len(out) + len(piece) > max_chars:
            break
        out += piece
    return out or cleaned[:max_chars] + "…"


def _query_excerpt(text: str, query: str, *, max_chars: int = SNIPPET_MAX_CHARS) -> str:
    """优先截取问题中的英文标识符附近内容，避免长表格把目标行截掉。"""
    cleaned = _clean(text)
    if len(cleaned) <= max_chars:
        return cleaned

    identifiers = sorted(
        {
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_.:-]{2,}", query or "")
        },
        key=len,
        reverse=True,
    )
    lowered = cleaned.lower()
    for identifier in identifiers:
        position = lowered.find(identifier)
        if position < 0:
            continue
        start = max(0, position - max_chars // 3)
        end = min(len(cleaned), start + max_chars)
        if end == len(cleaned):
            start = max(0, end - max_chars)
        excerpt = cleaned[start:end].strip()
        return ("…" if start else "") + excerpt + ("…" if end < len(cleaned) else "")

    return _first_sentences(cleaned, max_chars=max_chars)


def _numbered_items(text: str) -> list[str]:
    """提取从 1 开始的连续编号清单，忽略前面的 Markdown 数字章节标题。"""
    matches = [
        (int(number), _clean(body))
        for number, body in re.findall(
            r"(?ms)^\s*(\d+)\.\s+(.*?)(?=^\s*\d+\.\s+|\Z)",
            text or "",
        )
    ]
    for start, (number, _body) in enumerate(matches):
        if number != 1:
            continue
        items: list[str] = []
        expected = 1
        for current, body in matches[start:]:
            if current != expected:
                break
            items.append(body)
            expected += 1
        if len(items) >= 2:
            return items
    return []


def _asks_for_complete_list(query: str) -> bool:
    cleaned = _clean(query).lower()
    return any(
        marker in cleaned
        for marker in ("约定", "规则", "步骤", "流程", "要求", "怎么做", "逐条", "列出")
    )


def _action_summary(item: str) -> str:
    """把编号项压缩成“标题 + 应采取的动作”，完整原文继续放在引用中。"""
    cleaned = _clean(item).replace("**", "").replace("`", "")
    label, separator, body = cleaned.partition("：")
    if not separator:
        label, separator, body = cleaned.partition(":")
    if not separator:
        return _first_sentences(cleaned, max_chars=120).rstrip("。；; ")

    sentences = [
        part.strip("。；; ")
        for part in re.split(r"(?<=[。；;！？!?])\s*", body)
        if part.strip("。；; ")
    ]
    action_markers = ("应", "必须", "建议", "需要", "需", "不可", "不要", "跳过", "改用", "剔除", "去重", "保留", "对齐")
    candidates = [part for part in sentences if any(marker in part for marker in action_markers)]
    detail = candidates[-1] if candidates else (sentences[0] if sentences else body)

    # 一句话里先讲背景再讲动作时，只保留最后的处理动作，避免把原因整段搬到正文。
    marker_positions = [detail.find(marker) for marker in action_markers if marker in detail]
    if marker_positions:
        action_at = min(position for position in marker_positions if position >= 0)
        boundary = max(detail.rfind("，", 0, action_at), detail.rfind(",", 0, action_at))
        if boundary >= 0:
            detail = detail[boundary + 1 :].strip()

    # 技术字段列表适合放在引用中；直接答案使用业务语言说明处理动作。
    detail = re.sub(r"按\s*\([^)]{10,}\)\s*去重", "按业务维度去重", detail)
    detail = detail.rstrip("。；; ")
    return f"{label.strip()}：{detail}"


def _compact_direct_answer(text: str) -> str:
    """限制模型正文长度与要点数量，防止把多个检索片段顺次拼接。"""
    cleaned = _clean(text)
    clauses = [
        part.strip()
        for part in re.split(r"(?<=[。；;！？!?])\s*", cleaned)
        if part.strip()
    ]
    selected: list[str] = []
    for clause in clauses[:DIRECT_ANSWER_MAX_CLAUSES]:
        remaining = DIRECT_ANSWER_MAX_CHARS - len("".join(selected))
        if remaining <= 0:
            break
        if len(clause) > remaining:
            fragment = clause[:remaining].rstrip("，,:：；;。 ")
            comma = max(fragment.rfind("，"), fragment.rfind(","))
            if comma >= max(20, len(fragment) // 2):
                fragment = fragment[:comma].rstrip()
            if fragment:
                selected.append(fragment)
            break
        selected.append(clause)

    answer = "".join(selected).strip()
    if not answer:
        answer = cleaned[:DIRECT_ANSWER_MAX_CHARS].strip()
    needs_period = bool(answer and answer[-1] not in "。！？!?")
    hard_limit = DIRECT_ANSWER_MAX_CHARS - 1 if needs_period else DIRECT_ANSWER_MAX_CHARS
    answer = answer[:hard_limit].rstrip("，,:：；;。 ")
    return answer + ("。" if answer and needs_period else "")


def _structured_list_answer(query: str, hits: list[RetrievalHit]) -> str | None:
    """清单问题逐项归纳处理动作，既不漏项，也不把整段原文搬进正文。"""
    if not _asks_for_complete_list(query):
        return None
    if not hits:
        return None
    items = _numbered_items(hits[0].content)
    if len(items) < 2:
        return None
    summaries = [_action_summary(item) for item in items]
    return "根据知识库，建议按以下方式处理：\n" + "\n".join(
        f"{index}. {summary}" for index, summary in enumerate(summaries, start=1)
    )


def _citation_excerpt(text: str, query: str) -> str:
    """引用默认简短；清单型问题保留完整编号项，确保用户能核对直接答案。"""
    items = _numbered_items(text) if _asks_for_complete_list(query) else []
    if len(items) >= 2:
        return " ".join(f"{index}. {item}" for index, item in enumerate(items, start=1))
    return _query_excerpt(text, query, max_chars=SNIPPET_MAX_CHARS)


def _point_of(hit: RetrievalHit) -> str:
    """把一条命中整理成"答案要点"：内容 + 引用编号由调用方补。"""
    section = f"（{hit.section_title}）" if hit.section_title else ""
    return f"{_first_sentences(hit.content)}{section}"


def _looks_insufficient_answer(text: str) -> bool:
    """识别模型明确拒答，避免文字说"材料不足"但状态仍被当作可回答。"""
    cleaned = _clean(text).lower()
    markers = (
        "检索到的材料不足以回答",
        "材料不足以回答",
        "信息不足以回答",
        "无法回答该问题",
        "无法直接回答",
        "insufficient evidence",
        "not enough information",
    )
    return any(marker in cleaned for marker in markers)


def _model_answer(
    query: str,
    hits: list[RetrievalHit],
    settings: Settings,
    citations: list[QaCitation],
    conversation_context: list[dict] | None = None,
) -> tuple[str, str] | None:
    """可选的模型归纳：只允许基于给定片段作答，引用不存在的编号直接丢弃。

    返回 (答案, 模型名)；模型不可用或输出不合法时返回 None，由检索式答案兜底。
    """
    from app.services.model_gateway import get_model_gateway

    gateway = get_model_gateway()
    profile = gateway.profile_for("knowledge_qa")
    if not profile.configured or not hits:
        return None

    digest = "\n".join(
        f"[{citation.index}] "
        f"{_query_excerpt(hit.content, query, max_chars=MODEL_CONTEXT_MAX_CHARS)}"
        for citation, hit in zip(citations[:8], hits[:8], strict=False)
    )
    context_digest = "\n".join(
        f"用户：{str(item.get('query') or '')[:300]}\n助手：{str(item.get('answer') or '')[:400]}"
        for item in (conversation_context or [])[-4:]
    )
    payload, usage = gateway.chat_json(
        "你是知识库问答助手。只依据给定的引用片段回答问题，"
        '输出 JSON：{"answer": "一段话回答", "used_citations": [引用的编号]}。'
        "片段里没有的信息一律不要说；若片段不足以回答，answer 写明'检索到的材料不足以回答'并说明缺什么。"
        "当引用中存在多条与问题直接相关的并列规则、步骤或约定时，必须完整覆盖，不要只回答前几项。"
        "直接答案面向普通业务用户：只回答当前问题，不扩展到只是相关但未被询问的章节；"
        "先归纳再表达，禁止逐句复制或顺次拼接原文；最多 4 个结论、280 个中文字符。"
        "日期、版本号、阈值和字段名仅在直接影响结论时保留，其他技术细节交给引用来源展示。"
        "禁止编造引用编号。",
        f"<对话上下文>\n{context_digest or '无'}\n</对话上下文>\n"
        f"<当前问题>{query}</当前问题>\n<引用片段>\n{digest}\n</引用片段>",
        fallback=lambda: {},
        max_tokens=600,
        profile=profile,
    )
    if usage.stub or not isinstance(payload, dict):
        return None
    answer = str(payload.get("answer") or "").strip()
    if not answer:
        return None
    # 只保留真实存在的引用编号：模型编造的编号一律丢弃（否则引用会"看起来有据"其实没有）
    valid = {item.index for item in citations}
    used = [int(item) for item in (payload.get("used_citations") or []) if str(item).isdigit()]
    bogus = [item for item in used if item not in valid]
    if bogus:
        logger.warning("模型返回了不存在的引用编号 %s，已丢弃", bogus)
    return _compact_direct_answer(answer), usage.model


def answer_question(
    session: Session,
    *,
    query: str,
    tenant_id: str,
    user_id: str,
    roles: list[str] | None,
    clearance: int,
    allowed_kb_codes: list[str],
    requested_kb_codes: list[str] | None = None,
    app_version: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    top_k: int = 6,
    use_model: bool = True,
    conversation_context: list[dict] | None = None,
    settings: Settings | None = None,
) -> QaAnswer:
    """回答一个问题：检索可见知识库 → 生成答案要点与引用。"""
    settings = settings or get_settings()
    started = time.perf_counter()
    query = (query or "").strip()
    if not query:
        raise QaPermissionError("问题不能为空", code="invalid_request")

    # ① 指标级密级：与诊断任务入口同一套判定（越权指标域直接拒绝）
    denied = domain_denials(
        session,
        tenant_id=tenant_id,
        roles=roles,
        user_id=user_id,
        clearance=clearance,
        query=query,
    )
    if denied:
        raise QaPermissionError(
            "问题涉及超出你密级上限的指标：" + "；".join(item.reason for item in denied),
            code="permission_denied",
        )

    # ② 知识库范围：显式请求越权库 → 明确拒绝（与文档列表接口一致）
    allowed = set(allowed_kb_codes or [])
    requested = [code for code in (requested_kb_codes or []) if code]
    if requested:
        outside = sorted(set(requested) - allowed)
        if outside:
            raise QaPermissionError(f"无权访问该知识库: {outside}", code="permission_denied")
        kb_codes = sorted(set(requested))
    else:
        kb_codes = sorted(allowed)
    if not kb_codes:
        raise QaPermissionError("当前角色没有被授权访问任何知识库", code="permission_denied")

    # ③ 连续追问：短问句或指代词需要带上最近问题，否则“它有哪些字段”无法召回上一轮主题。
    context = conversation_context or []
    needs_context = bool(context) and (
        len(query) < 24
        or any(marker in query for marker in ("它", "这个", "上述", "前面", "刚才", "继续", "其中", "该"))
    )
    retrieval_query = query
    if needs_context:
        previous_queries = [str(item.get("query") or "").strip() for item in context[-2:]]
        retrieval_query = "；".join([item for item in previous_queries if item] + [query])

    # ④ 检索（可见范围 + 密级上限都在检索 SQL 里生效）
    service = RetrievalService()
    hits = service.search(
        session,
        query=retrieval_query,
        tenant_id=tenant_id,
        kb_codes=kb_codes,
        max_security_rank=clearance,
        top_k=top_k,
        app_version=app_version,
        date_from=date_from,
        date_to=date_to,
    )

    citations = [
        QaCitation(
            index=index,
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            kb_code=hit.kb_code,
            source_name=hit.source_name,
            page=hit.page,
            document_version=hit.document_version,
            score=hit.relevance_score,
            snippet=_citation_excerpt(hit.content, query),
        )
        for index, hit in enumerate(hits[:top_k], start=1)
    ]

    answer = QaAnswer(
        query=query,
        answer="",
        citations=citations,
        knowledge_bases=sorted({hit.kb_code for hit in hits}),
        latency_ms=0,
    )

    # ⑤ 命中不足：用**与诊断工作流同一套覆盖率口径**判断，而不是自己拍阈值。
    # 各写一份阈值的结果是"问答说能答、诊断说证据不足"，用户会认为系统自相矛盾。
    from app.services.answerability import measure_coverage

    top_score = max((hit.relevance_score for hit in hits), default=0.0)
    coverage = measure_coverage(
        retrieval_query,
        [hit.content for hit in hits],
        top_score=top_score,
        settings=settings,
    )
    answer.coverage = coverage.as_dict()
    if not hits or not coverage.answerable:
        answer.insufficient = True
        answer.missing = [
            f"在 {', '.join(kb_codes)} 中没有检索到与该问题直接相关的内容",
            f"问题关键词覆盖 {coverage.matched}/{coverage.terms}"
            + (f"，未覆盖：{'、'.join(coverage.missing_terms[:6])}" if coverage.missing_terms else ""),
        ]
        answer.answer = (
            "知识库里没有检索到能回答这个问题的材料。"
            f"（已搜索：{'、'.join(kb_codes)}；问题关键词覆盖 {coverage.coverage:.0%}，"
            f"阈值 {coverage.threshold:.0%}）"
            "建议换一种问法、放宽版本/时间范围，或先上传相关资料。"
        )
        answer.latency_ms = int((time.perf_counter() - started) * 1000)
        return answer

    points = [
        f"[{citation.index}] {_point_of(hit)}"
        for citation, hit in list(zip(citations, hits, strict=False))[:MAX_POINTS]
    ]
    answer.points = points

    # ⑥ 可选模型归纳（失败就用检索式答案，不影响可用性）
    structured = _structured_list_answer(query, hits)
    refined = (
        _model_answer(query, hits, settings, citations, conversation_context)
        if use_model and structured is None
        else None
    )
    if structured is not None:
        answer.answer = structured
        answer.mode = "extractive"
    elif refined is not None:
        answer.answer, answer.model = refined
        answer.mode = "model"
        # 模型可能在规则覆盖率达标时仍判断"材料不足"。这时必须把状态归一化，
        # 否则前端会把模型拒答文本和检索片段继续展示成正常答案。
        if _looks_insufficient_answer(answer.answer):
            answer.insufficient = True
            answer.points = []
            answer.missing = [
                "模型判断当前引用片段不足以直接回答该问题",
                "请补充能明确描述问题口径、触发条件或适用范围的资料",
            ]
    else:
        answer.answer = "根据知识库中的以下材料（点编号可查看原文片段）："
        answer.mode = "extractive"

    answer.latency_ms = int((time.perf_counter() - started) * 1000)
    return answer


__all__ = [
    "QaAnswer",
    "QaCitation",
    "QaPermissionError",
    "answer_question",
]

"""引用构建与校验（设计文档 9.4 / 10 节）。

证据在进入报告前必须：
1. 可定位（document_id + chunk_id + 页码 + 版本）
2. 权限已校验（permission_checked=True）
3. 与陈述一一绑定（statement_id）
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import EvidenceKind, StatementType
from app.rag.retrieval import RetrievalHit


@dataclass
class CitationDraft:
    statement_id: str
    statement_type: StatementType
    source_kind: EvidenceKind
    chunk_id: str | None = None
    document_id: str | None = None
    kb_code: str | None = None
    source_name: str | None = None
    page: int | None = None
    document_version: str | None = None
    content: str | None = None
    relevance_score: float | None = None
    metric_ref: dict | None = None
    permission_checked: bool = True

    def as_record(self, task_id: str, report_id: str | None = None) -> dict:
        return {
            "task_id": task_id,
            "report_id": report_id,
            "statement_id": self.statement_id,
            "statement_type": self.statement_type.value,
            "source_kind": self.source_kind.value,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "kb_code": self.kb_code,
            "source_name": self.source_name,
            "page": self.page,
            "document_version": self.document_version,
            "content": (self.content or "")[:2000],
            "relevance_score": self.relevance_score,
            "metric_ref": self.metric_ref,
            "permission_checked": self.permission_checked,
        }


def citation_from_hit(
    hit: RetrievalHit,
    *,
    statement_id: str,
    statement_type: StatementType = StatementType.FACT,
) -> CitationDraft:
    return CitationDraft(
        statement_id=statement_id,
        statement_type=statement_type,
        source_kind=EvidenceKind.DOCUMENT,
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        kb_code=hit.kb_code,
        source_name=hit.source_name,
        page=hit.page,
        document_version=hit.document_version,
        content=hit.content,
        relevance_score=hit.relevance_score,
        permission_checked=hit.permission_checked,
    )


def citation_from_metric(
    *,
    statement_id: str,
    metric_ref: dict,
    statement_type: StatementType = StatementType.FACT,
) -> CitationDraft:
    """指标类证据：记录查询参数与数据版本，保证可复现。"""
    return CitationDraft(
        statement_id=statement_id,
        statement_type=statement_type,
        source_kind=EvidenceKind.METRIC,
        source_name=metric_ref.get("metric_name"),
        content=metric_ref.get("summary"),
        metric_ref=metric_ref,
        permission_checked=True,
    )


def format_citation_marks(citations: list[dict]) -> dict[str, str]:
    """生成 [1] [2] 形式的引用标记与来源说明。"""
    marks: dict[str, str] = {}
    for index, item in enumerate(citations, start=1):
        source = item.get("source_name") or item.get("metric_ref", {}).get("metric_name") or "未知来源"
        page = f" p{item['page']}" if item.get("page") else ""
        version = f" ({item['document_version']})" if item.get("document_version") else ""
        marks[str(index)] = f"[{index}] {source}{page}{version}"
    return marks


def dedupe_citations(citations: list[CitationDraft]) -> list[CitationDraft]:
    seen: set[tuple] = set()
    result: list[CitationDraft] = []
    for item in citations:
        key = (
            item.statement_id,
            item.chunk_id,
            item.source_kind.value,
            str(item.metric_ref),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def validate_citation_coverage(statements: list[dict], citations: list[CitationDraft]) -> list[str]:
    """校验：每条事实/推断都必须有引用，缺失则返回问题说明。"""
    problems: list[str] = []
    covered = {c.statement_id for c in citations}
    for statement in statements:
        if statement.get("type") in {StatementType.FACT.value, StatementType.INFERENCE.value}:
            if statement.get("id") not in covered:
                problems.append(f"陈述 {statement.get('id')} 缺少引用证据")
    return problems

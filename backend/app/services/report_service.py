"""报告持久化与渲染（设计文档 10 / 12 节）。

- 报告正文按"事实 / 推断 / 假设 / 建议"四类分开存储；
- 每条陈述与证据的绑定关系写入 `evidence_citations`，保证"关键结论可定位"；
- 同时渲染一份 Markdown，供导出与人工审核阅读。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.logging_utils import get_logger
from app.models.enums import EvidenceKind, StatementType
from app.models.workflow import EvidenceCitation, Report

logger = get_logger(__name__)

STATEMENT_SECTIONS = (
    ("facts", StatementType.FACT, "事实"),
    ("inferences", StatementType.INFERENCE, "推断"),
    ("hypotheses", StatementType.HYPOTHESIS, "假设"),
    ("recommendations", StatementType.RECOMMENDATION, "建议"),
)


def next_version(session: Session, task_id: str) -> int:
    current = (
        session.query(func.max(Report.version)).filter(Report.task_id == task_id).scalar()
    )
    return int(current or 0) + 1


def persist_report(
    session: Session,
    *,
    task_id: str,
    tenant_id: str,
    report: dict,
    evidence: list[dict],
    created_by: str | None = None,
) -> Report:
    """写入报告与引用关系。返回 Report 行（未 commit）。"""
    evidence_by_id = {item.get("evidence_id"): item for item in evidence}
    version = next_version(session, task_id)

    row = Report(
        task_id=task_id,
        tenant_id=tenant_id,
        version=version,
        summary=report.get("summary"),
        impact_scope=report.get("impact_scope") or [],
        facts=report.get("facts") or [],
        inferences=report.get("inferences") or [],
        hypotheses=report.get("hypotheses") or [],
        evidence=evidence,
        conflicts=report.get("conflicts") or [],
        recommendations=report.get("recommendations") or [],
        priority=report.get("priority"),
        confidence=report.get("confidence"),
        needs_human_review=bool(report.get("needs_human_review")),
        risk_level=report.get("risk_level") or "low",
        approval_status=report.get("approval_status") or "not_required",
        content_json=report,
        created_by=created_by,
    )
    session.add(row)
    session.flush()

    row.content_md = render_markdown(report, evidence_by_id, report_id=row.id, version=version)
    _persist_citations(session, task_id=task_id, report_id=row.id, report=report, evidence_by_id=evidence_by_id)
    session.flush()
    logger.info("报告已生成 report_id=%s task_id=%s version=%s", row.id, task_id, version)
    return row


def _persist_citations(
    session: Session,
    *,
    task_id: str,
    report_id: str,
    report: dict,
    evidence_by_id: dict[str, dict],
) -> int:
    count = 0
    for section, statement_type, _label in STATEMENT_SECTIONS:
        for statement in report.get(section) or []:
            statement_id = statement.get("id") or f"{section}-{count + 1}"
            for ref in statement.get("evidence_refs") or []:
                item = evidence_by_id.get(ref)
                if not item:
                    # 引用不存在的证据：记为未通过权限/存在性校验，不写入引用表
                    continue
                session.add(
                    EvidenceCitation(
                        task_id=task_id,
                        report_id=report_id,
                        statement_id=statement_id,
                        statement_type=statement_type.value,
                        source_kind=item.get("source_kind") or EvidenceKind.DOCUMENT.value,
                        chunk_id=item.get("chunk_id"),
                        document_id=item.get("document_id"),
                        kb_code=item.get("knowledge_base") or item.get("kb_code"),
                        source_name=item.get("source_name"),
                        page=item.get("page"),
                        document_version=item.get("document_version"),
                        content=(item.get("content") or "")[:2000],
                        relevance_score=item.get("relevance_score"),
                        metric_ref=_metric_ref(item) if item.get("source_kind") == "metric" else None,
                        permission_checked=bool(item.get("permission_checked", True)),
                    )
                )
                count += 1
    return count


def _metric_ref(item: dict) -> dict:
    return {
        "metric_name": item.get("metric_name"),
        "capability": item.get("capability"),
        "params": item.get("params"),
        "summary": item.get("summary"),
        "dataset_version": item.get("dataset_version"),
        "source_version": item.get("source_version"),
        "quality_flags": item.get("quality_flags") or [],
        "warnings": item.get("warnings") or [],
    }


def citation_marks(citations: list[EvidenceCitation] | list[dict], evidence: list[dict]) -> dict[str, str]:
    """生成 "E1" -> "[1] 来源名 p2 (v3.2.0)" 形式的引用说明。"""
    by_id = {item.get("evidence_id"): item for item in evidence}
    marks: dict[str, str] = {}
    for index, item in enumerate(evidence, start=1):
        evidence_id = item.get("evidence_id") or f"E{index}"
        source = item.get("source_name") or (item.get("metric_name") and f"指标:{item['metric_name']}") or "未知来源"
        page = f" p{item['page']}" if item.get("page") else ""
        version = f" ({item['document_version']})" if item.get("document_version") else ""
        dataset = f" [{item['dataset_version']}]" if item.get("source_kind") == "metric" and item.get("dataset_version") else ""
        marks[evidence_id] = f"[{index}] {source}{page}{version}{dataset}"
    _ = by_id
    return marks


def render_markdown(
    report: dict,
    evidence_by_id: dict[str, dict] | None = None,
    *,
    report_id: str | None = None,
    version: int | None = None,
) -> str:
    """把结构化报告渲染成可读 Markdown（导出与人工审核共用）。"""
    evidence_by_id = evidence_by_id or {item.get("evidence_id"): item for item in report.get("evidence") or []}
    marks = citation_marks([], report.get("evidence") or [])
    order = {key: index for index, key in enumerate(marks)}

    def refs(statement: dict) -> str:
        items = [r for r in statement.get("evidence_refs") or [] if r in marks]
        if not items:
            return ""
        labels = [marks[r].split(" ")[0] for r in items]
        return " " + " ".join(labels)

    lines: list[str] = ["# 产品问题诊断报告", ""]
    if report_id:
        lines.append(f"- 报告 ID：`{report_id}`" + (f"（v{version}）" if version else ""))
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 优先级：{report.get('priority') or '未定'}")
    confidence = report.get("confidence")
    lines.append(f"- 置信度：{confidence:.2f}" if isinstance(confidence, (int, float)) else "- 置信度：未评估")
    lines.append(f"- 风险等级：{report.get('risk_level') or 'low'}")
    lines.append(f"- 审核状态：{report.get('approval_status') or 'not_required'}")
    lines.append("")
    lines.append("## 结论摘要")
    lines.append("")
    lines.append(report.get("summary") or "（无）")

    if report.get("impact_scope"):
        lines.append("")
        lines.append("## 影响范围")
        lines.append("")
        lines.append("| 维度 | 取值 | 证据 |")
        lines.append("|---|---|---|")
        for item in report["impact_scope"]:
            lines.append(
                f"| {item.get('dimension')} | {item.get('value')} | {refs(item).strip() or '-'} |"
            )

    for section, _type, label in STATEMENT_SECTIONS:
        items = report.get(section) or []
        if not items:
            continue
        lines.append("")
        lines.append(f"## {label}")
        lines.append("")
        for item in items:
            prefix = f"**{item.get('id')}** " if item.get("id") else ""
            lines.append(f"- {prefix}{item.get('text')}{refs(item)}")
            if item.get("basis"):
                lines.append(f"  - 依据：{item['basis']}")
            if item.get("verify_by"):
                lines.append(f"  - 验证方式：{item['verify_by']}")
            if item.get("priority") and section == "recommendations":
                lines.append(f"  - 优先级：{item['priority']}｜责任角色：{item.get('owner_role') or '-'}")

    conflicts = report.get("conflicts") or []
    if conflicts:
        lines.append("")
        lines.append("## 证据冲突（未强行合并）")
        lines.append("")
        for conflict in conflicts:
            lines.append(f"### {conflict.get('topic') or conflict.get('id')}")
            lines.append("")
            a = conflict.get("statement_a") or {}
            b = conflict.get("statement_b") or {}
            lines.append(f"- 来源 A（{a.get('agent_code')}）：{a.get('text')}")
            lines.append(f"- 来源 B（{b.get('agent_code')}）：{b.get('text')}")
            resolution = conflict.get("resolution")
            if resolution:
                lines.append(
                    f"- 采用：`{resolution.get('adopted_statement')}`"
                    f"（{resolution.get('adopted_source')}）；原因：{resolution.get('reason')}"
                )
                lines.append(f"- 来源优先级规则：{resolution.get('priority_rule')}")
            else:
                lines.append("- 采用：待人工裁决")

    warnings = report.get("warnings") or []
    gaps = report.get("data_gaps") or []
    if warnings or gaps:
        lines.append("")
        lines.append("## 不确定性与数据缺口")
        lines.append("")
        for item in gaps:
            lines.append(f"- 数据缺口：{item}")
        for item in warnings:
            lines.append(f"- 提示：{item}")

    if report.get("evidence"):
        lines.append("")
        lines.append("## 证据清单")
        lines.append("")
        for key in sorted(marks, key=lambda k: order.get(k, 999)):
            lines.append(f"- {marks[key]} — `{key}`")

    if isinstance(confidence, (int, float)) and confidence < 0.6:
        lines.append("")
        lines.append("> 提示：本次诊断置信度偏低，建议补充材料或由人工复核后再做决策。")

    return "\n".join(lines).strip() + "\n"

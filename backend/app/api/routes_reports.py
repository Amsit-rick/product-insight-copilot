"""报告与引用接口。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.workflow import EvidenceCitation, Report
from app.schemas.report import CitationOut, ReportOut
from app.services.report_service import citation_marks

router = APIRouter(prefix="/reports", tags=["诊断报告"])


def build_report_out(session: Session, report: Report) -> ReportOut:
    citations = (
        session.query(EvidenceCitation)
        .filter(EvidenceCitation.report_id == report.id)
        .order_by(EvidenceCitation.created_at.asc())
        .all()
    )
    content = report.content_json or {}
    out = ReportOut.model_validate(report)
    out.citations = [CitationOut.model_validate(item) for item in citations]
    out.citation_marks = citation_marks(citations, report.evidence or [])
    out.validation_problems = content.get("validation_problems") or []
    out.data_gaps = content.get("data_gaps") or []
    out.warnings = content.get("warnings") or []
    out.reflection = content.get("reflection") or {}
    return out


@router.get("/{report_id}", response_model=ReportOut, summary="报告详情（含引用）")
def get_report(
    report_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> ReportOut:
    principal.require("report:read")
    report = session.get(Report, report_id)
    if report is None or report.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="报告不存在")
    return build_report_out(session, report)


@router.get("/{report_id}/export", summary="导出报告（markdown / json）")
def export_report(
    report_id: str,
    format: str = Query(default="md", pattern="^(md|json)$"),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
):
    principal.require("report:read")
    report = session.get(Report, report_id)
    if report is None or report.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="报告不存在")
    if format == "json":
        payload = build_report_out(session, report)
        return PlainTextResponse(
            content=json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="report-{report_id}.json"'},
        )
    return PlainTextResponse(
        content=report.content_md or "",
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="report-{report_id}.md"'},
    )


@router.get("", summary="按任务查询报告列表")
def list_reports(
    task_id: str = Query(...),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("report:read")
    reports = (
        session.query(Report)
        .filter(Report.task_id == task_id)
        .filter(Report.tenant_id == principal.tenant_id)
        .order_by(Report.version.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": item.id,
                "task_id": item.task_id,
                "version": item.version,
                "summary": item.summary,
                "priority": item.priority,
                "confidence": item.confidence,
                "risk_level": item.risk_level,
                "approval_status": item.approval_status,
                "needs_human_review": item.needs_human_review,
                "conflict_count": len(item.conflicts or []),
                "evidence_count": len(item.evidence or []),
                "created_at": item.created_at,
            }
            for item in reports
        ],
        "total": len(reports),
    }

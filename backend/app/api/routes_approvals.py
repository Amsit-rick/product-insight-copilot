"""审批接口：待审核列表、审核详情与审核结论。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.enums import ApprovalStatus
from app.models.workflow import AnalysisTask, Approval
from app.schemas.approval import ApprovalOut
from app.schemas.task import TaskApproveRequest, TaskOut
from app.services.approval_service import apply_decision
from app.services.task_service import (
    TaskConflict,
    TaskNotFound,
    TaskRevisionConflict,
    TaskService,
)
from app.services.workflow_runner import get_runner
from app.config import get_settings

router = APIRouter(prefix="/approvals", tags=["人工审核"])


@router.get("", summary="审核记录列表（默认待审核）")
def list_approvals(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    status_filter: str | None = Query(default=ApprovalStatus.PENDING.value, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    principal.require("task:read")
    query = (
        session.query(Approval)
        .join(AnalysisTask, AnalysisTask.id == Approval.task_id)
        .filter(AnalysisTask.tenant_id == principal.tenant_id)
    )
    if status_filter:
        query = query.filter(Approval.status == status_filter)
    rows = query.order_by(Approval.created_at.desc()).limit(limit).all()
    items = []
    for row in rows:
        task = session.get(AnalysisTask, row.task_id)
        items.append(
            {
                **ApprovalOut.model_validate(row).model_dump(),
                "task_query": task.query if task else None,
                "task_risk_level": task.risk_level if task else None,
                "task_state_revision": int(task.state_revision or 0) if task else None,
                "waiting_reason": task.waiting_reason if task else None,
                "request": (row.diff or {}).get("request"),
            }
        )
    return {"items": items, "total": len(items)}


@router.get("/{approval_id}", response_model=ApprovalOut, summary="审核详情")
def get_approval(
    approval_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> ApprovalOut:
    principal.require("task:read")
    row = session.get(Approval, approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail="审核记录不存在")
    task = session.get(AnalysisTask, row.task_id)
    if task is None or task.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="审核记录不存在")
    return ApprovalOut.model_validate(row).model_copy(
        update={"task_state_revision": int(task.state_revision or 0)}
    )


@router.post("/{approval_id}/decision", response_model=TaskOut, summary="提交审核决定并恢复工作流")
def decide(
    approval_id: str,
    payload: TaskApproveRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> TaskOut:
    principal.require("task:approve")
    row = session.get(Approval, approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail="审核记录不存在")
    service = TaskService(session)
    try:
        task = service.get(row.task_id, tenant_id=principal.tenant_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if task.status != "waiting_human":
        raise HTTPException(
            status_code=409,
            detail={"code": "not_waiting_human", "message": f"任务状态为 {task.status}，无需审核"},
        )

    try:
        task = service.claim_revision(
            task.id,
            tenant_id=principal.tenant_id,
            expected_revision=payload.expected_revision,
            allowed_statuses={"waiting_human"},
            invalid_status_message=f"任务状态为 {task.status}，无需审核",
        )
    except TaskRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    decision = {
        "action": payload.action,
        "reviewer_id": principal.user.id,
        "comment": payload.comment,
        "modifications": payload.modifications or {},
        "_expected_revision": task.state_revision,
    }
    # 审核记录先落库，再恢复工作流（工作流恢复时会幂等复用该记录）
    apply_decision(
        session,
        task_id=task.id,
        action=payload.action,
        reviewer_id=principal.user.id,
        comment=payload.comment,
        modifications=payload.modifications,
    )
    runner = get_runner()
    inline = get_settings().task_run_inline
    if not inline:
        task.status = "running"
        task.waiting_reason = None
    session.commit()

    if inline:
        runner.resume(task.id, decision)
    else:
        runner.submit_resume(task.id, decision)
    session.expire_all()
    return TaskOut(**service.to_out(service.get(task.id)))

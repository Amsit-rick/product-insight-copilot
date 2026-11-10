"""人工审批服务（设计文档 4.2 / 7.1 / 11 节）。

- `ensure_pending_approval`：进入人工审核时写一条待审核记录（幂等，支持断点恢复重复执行）；
- `apply_decision`：记录审批结论、人工修改 diff 与审核人，全部进审计；
- 审批不删除任何证据，驳回也保留完整留痕。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.logging_utils import get_logger
from app.models.enums import ApprovalAction, ApprovalStatus
from app.models.workflow import Approval

logger = get_logger(__name__)


def ensure_pending_approval(
    session: Session,
    *,
    task_id: str,
    reason: str,
    report_id: str | None = None,
    payload: dict | None = None,
) -> Approval:
    """幂等地创建待审核记录：同一任务已有 pending 记录时直接复用。"""
    existing = (
        session.query(Approval)
        .filter(Approval.task_id == task_id)
        .filter(Approval.status == ApprovalStatus.PENDING.value)
        .order_by(Approval.created_at.desc())
        .first()
    )
    if existing is not None:
        if payload and not existing.diff:
            existing.diff = {"request": payload, "reason": reason}
        from app.services.task_execution import TaskExecutionService

        execution = TaskExecutionService(session)
        execution.start_unit(
            task_id=task_id,
            name="人工审批",
            unit_type="approval",
            owner_type="approval",
            owner_ref_id=existing.id,
            status="waiting",
            failure_policy="block_parent",
            metadata={"reason": reason[:500]},
        )
        execution.set_root_status(task_id, "waiting")
        return existing
    row = Approval(
        task_id=task_id,
        report_id=report_id,
        status=ApprovalStatus.PENDING.value,
        diff={"request": payload or {}, "reason": reason},
    )
    session.add(row)
    session.flush()
    from app.services.task_execution import TaskExecutionService

    execution = TaskExecutionService(session)
    execution.start_unit(
        task_id=task_id,
        name="人工审批",
        unit_type="approval",
        owner_type="approval",
        owner_ref_id=row.id,
        status="waiting",
        failure_policy="block_parent",
        metadata={"reason": reason[:500]},
    )
    execution.set_root_status(task_id, "waiting")
    logger.info("创建待审核记录 task_id=%s approval_id=%s", task_id, row.id)
    return row


def apply_decision(
    session: Session,
    *,
    task_id: str,
    action: str,
    reviewer_id: str | None,
    comment: str | None = None,
    modifications: dict | None = None,
) -> Approval:
    """落地审核结论。返回更新后的 Approval 行。"""
    if action not in {item.value for item in ApprovalAction}:
        raise ValueError(f"非法审核动作: {action}")

    row = (
        session.query(Approval)
        .filter(Approval.task_id == task_id)
        .order_by(Approval.created_at.desc())
        .first()
    )
    status = {
        ApprovalAction.APPROVE.value: ApprovalStatus.APPROVED.value,
        ApprovalAction.REJECT.value: ApprovalStatus.REJECTED.value,
        ApprovalAction.MODIFY.value: ApprovalStatus.MODIFIED.value,
    }[action]

    if row is None:
        row = Approval(task_id=task_id, diff={})
        session.add(row)

    diff = dict(row.diff or {})
    diff["decision"] = {
        "action": action,
        "comment": comment,
        "modifications": modifications or {},
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    row.status = status
    row.action = action
    row.reviewer_id = reviewer_id
    row.comment = comment
    row.diff = diff
    row.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.flush()
    from app.services.task_execution import TaskExecutionService

    execution = TaskExecutionService(session)
    execution.start_unit(
        task_id=task_id,
        name="人工审批",
        unit_type="approval",
        owner_type="approval",
        owner_ref_id=row.id,
        status="waiting",
        failure_policy="block_parent",
    )
    execution.finish_owner(
        task_id=task_id,
        owner_type="approval",
        owner_ref_id=row.id,
        status="succeeded" if action != ApprovalAction.REJECT.value else "rejected",
    )
    execution.set_root_status(task_id, "running")
    logger.info("审核结论落地 task_id=%s action=%s status=%s", task_id, action, status)
    return row


def approval_status_for(action: str | None) -> str:
    if action is None:
        return ApprovalStatus.NOT_REQUIRED.value
    return {
        ApprovalAction.APPROVE.value: ApprovalStatus.APPROVED.value,
        ApprovalAction.REJECT.value: ApprovalStatus.REJECTED.value,
        ApprovalAction.MODIFY.value: ApprovalStatus.MODIFIED.value,
    }.get(action, ApprovalStatus.PENDING.value)


def latest_approval(session: Session, task_id: str) -> Approval | None:
    return (
        session.query(Approval)
        .filter(Approval.task_id == task_id)
        .order_by(Approval.created_at.desc())
        .first()
    )

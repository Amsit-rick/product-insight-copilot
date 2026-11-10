"""Persistent task execution tree, ownership and propagation policies."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.enums import TaskStatus
from app.models.workflow import AnalysisTask, TaskExecutionUnit

UNIT_TYPES = {"workflow", "agent", "tool", "approval", "background"}
OWNER_TYPES = {"workflow", "agent_run", "tool_call", "approval", "system"}
EXECUTION_MODES = {"attached", "background"}
CANCELLATION_POLICIES = {"cascade", "independent"}
FAILURE_POLICIES = {"continue", "block_parent", "fail_parent"}
ACTIVE_STATUSES = {"pending", "running", "waiting", "cancel_requested"}
FINISHED_STATUSES = {"succeeded", "failed", "cancelled", "rejected", "skipped"}


def root_status_for_task(status: str) -> str:
    return {
        TaskStatus.PENDING.value: "pending",
        TaskStatus.RUNNING.value: "running",
        TaskStatus.WAITING_HUMAN.value: "waiting",
        TaskStatus.COMPLETED.value: "succeeded",
        TaskStatus.FAILED.value: "failed",
        TaskStatus.CANCELLED.value: "cancelled",
    }.get(status, "pending")


class TaskExecutionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_root(self, task: AnalysisTask) -> TaskExecutionUnit:
        existing = self.unit_for_owner(
            task_id=task.id,
            owner_type="workflow",
            owner_ref_id=task.id,
        )
        if existing is not None:
            return existing
        row = TaskExecutionUnit(
            id=task.id,
            task_id=task.id,
            parent_unit_id=None,
            name=task.title or "主任务",
            unit_type="workflow",
            owner_type="workflow",
            owner_ref_id=task.id,
            status=root_status_for_task(task.status),
            execution_mode="attached",
            cancellation_policy="cascade",
            failure_policy="continue",
            metadata_json={"query": task.query[:500]},
            started_at=task.started_at,
            finished_at=task.finished_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def sync_root(self, task: AnalysisTask) -> TaskExecutionUnit:
        root = self.ensure_root(task)
        root.status = root_status_for_task(task.status)
        root.started_at = task.started_at or root.started_at
        root.finished_at = task.finished_at
        self.session.flush()
        return root

    def set_root_status(self, task_id: str, status: str) -> TaskExecutionUnit | None:
        root = self.unit_for_owner(
            task_id=task_id,
            owner_type="workflow",
            owner_ref_id=task_id,
        )
        if root is None:
            task = self.session.get(AnalysisTask, task_id)
            if task is None:
                return None
            root = self.ensure_root(task)
        root.status = status
        if status == "running" and root.started_at is None:
            root.started_at = utcnow()
        if status in FINISHED_STATUSES:
            root.finished_at = utcnow()
        self.session.flush()
        return root

    def unit_for_owner(
        self,
        *,
        task_id: str,
        owner_type: str,
        owner_ref_id: str,
    ) -> TaskExecutionUnit | None:
        return (
            self.session.query(TaskExecutionUnit)
            .filter(TaskExecutionUnit.task_id == task_id)
            .filter(TaskExecutionUnit.owner_type == owner_type)
            .filter(TaskExecutionUnit.owner_ref_id == owner_ref_id)
            .first()
        )

    def start_unit(
        self,
        *,
        task_id: str,
        name: str,
        unit_type: str,
        owner_type: str,
        owner_ref_id: str,
        parent_unit_id: str | None = None,
        status: str = "running",
        execution_mode: str = "attached",
        cancellation_policy: str = "cascade",
        failure_policy: str = "continue",
        metadata: dict | None = None,
    ) -> TaskExecutionUnit:
        self._validate(
            unit_type=unit_type,
            owner_type=owner_type,
            execution_mode=execution_mode,
            cancellation_policy=cancellation_policy,
            failure_policy=failure_policy,
        )
        existing = self.unit_for_owner(
            task_id=task_id,
            owner_type=owner_type,
            owner_ref_id=owner_ref_id,
        )
        if existing is not None:
            return existing
        task = self.session.get(AnalysisTask, task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        root = self.ensure_root(task)
        row = TaskExecutionUnit(
            task_id=task_id,
            parent_unit_id=parent_unit_id or root.id,
            name=name[:255],
            unit_type=unit_type,
            owner_type=owner_type,
            owner_ref_id=owner_ref_id,
            status=status,
            execution_mode=execution_mode,
            cancellation_policy=cancellation_policy,
            failure_policy=failure_policy,
            metadata_json=metadata or {},
            started_at=utcnow() if status == "running" else None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def finish_owner(
        self,
        *,
        task_id: str,
        owner_type: str,
        owner_ref_id: str,
        status: str,
        error: str | None = None,
    ) -> TaskExecutionUnit | None:
        row = self.unit_for_owner(
            task_id=task_id,
            owner_type=owner_type,
            owner_ref_id=owner_ref_id,
        )
        if row is None:
            return None
        return self.finish_unit(row.id, status=status, error=error)

    def finish_unit(
        self,
        unit_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> TaskExecutionUnit:
        row = self.session.get(TaskExecutionUnit, unit_id)
        if row is None:
            raise ValueError(f"执行单元不存在: {unit_id}")
        row.status = status
        if status in FINISHED_STATUSES:
            row.finished_at = utcnow()
        if error:
            row.metadata_json = {**(row.metadata_json or {}), "error": error[:1000]}
        self.session.flush()
        if status == "failed":
            self._apply_failure_policy(row)
        return row

    def request_cancel_descendants(self, task_id: str, *, reason: str) -> list[str]:
        rows = (
            self.session.query(TaskExecutionUnit)
            .filter(TaskExecutionUnit.task_id == task_id)
            .filter(TaskExecutionUnit.parent_unit_id.isnot(None))
            .filter(TaskExecutionUnit.status.in_(ACTIVE_STATUSES))
            .all()
        )
        affected: list[str] = []
        for row in rows:
            if row.cancellation_policy == "independent":
                continue
            row.status = "cancel_requested"
            row.metadata_json = {
                **(row.metadata_json or {}),
                "cancel_reason": reason[:500],
            }
            affected.append(row.id)
        root = self.set_root_status(task_id, "cancel_requested")
        if root is not None:
            root.metadata_json = {**(root.metadata_json or {}), "cancel_reason": reason[:500]}
        self.session.flush()
        return affected

    def finalize_cancel_descendants(self, task_id: str) -> list[str]:
        rows = (
            self.session.query(TaskExecutionUnit)
            .filter(TaskExecutionUnit.task_id == task_id)
            .filter(TaskExecutionUnit.parent_unit_id.isnot(None))
            .filter(TaskExecutionUnit.cancellation_policy == "cascade")
            .filter(TaskExecutionUnit.status.in_(ACTIVE_STATUSES))
            .all()
        )
        for row in rows:
            row.status = "cancelled"
            row.finished_at = utcnow()
        self.set_root_status(task_id, "cancelled")
        self.session.flush()
        return [row.id for row in rows]

    def tree(self, task_id: str) -> dict:
        task = self.session.get(AnalysisTask, task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        root = self.ensure_root(task)
        rows = (
            self.session.query(TaskExecutionUnit)
            .filter(TaskExecutionUnit.task_id == task_id)
            .order_by(TaskExecutionUnit.created_at.asc(), TaskExecutionUnit.id.asc())
            .all()
        )
        children: dict[str | None, list[TaskExecutionUnit]] = defaultdict(list)
        for row in rows:
            children[row.parent_unit_id].append(row)

        def render(row: TaskExecutionUnit) -> dict:
            return {
                "id": row.id,
                "task_id": row.task_id,
                "parent_unit_id": row.parent_unit_id,
                "name": row.name,
                "unit_type": row.unit_type,
                "owner_type": row.owner_type,
                "owner_ref_id": row.owner_ref_id,
                "status": row.status,
                "execution_mode": row.execution_mode,
                "cancellation_policy": row.cancellation_policy,
                "failure_policy": row.failure_policy,
                "metadata": row.metadata_json or {},
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "children": [render(child) for child in children.get(row.id, [])],
            }

        return render(root)

    def _apply_failure_policy(self, row: TaskExecutionUnit) -> None:
        if not row.parent_unit_id or row.failure_policy == "continue":
            return
        parent = self.session.get(TaskExecutionUnit, row.parent_unit_id)
        if parent is None:
            return
        if row.failure_policy == "block_parent":
            parent.status = "waiting"
            parent.metadata_json = {
                **(parent.metadata_json or {}),
                "blocked_by_unit_id": row.id,
            }
        elif row.failure_policy == "fail_parent":
            parent.status = "failed"
            parent.finished_at = utcnow()
            parent.metadata_json = {
                **(parent.metadata_json or {}),
                "failed_by_unit_id": row.id,
            }
        self.session.flush()

    @staticmethod
    def _validate(
        *,
        unit_type: str,
        owner_type: str,
        execution_mode: str,
        cancellation_policy: str,
        failure_policy: str,
    ) -> None:
        checks = (
            (unit_type, UNIT_TYPES, "unit_type"),
            (owner_type, OWNER_TYPES, "owner_type"),
            (execution_mode, EXECUTION_MODES, "execution_mode"),
            (cancellation_policy, CANCELLATION_POLICIES, "cancellation_policy"),
            (failure_policy, FAILURE_POLICIES, "failure_policy"),
        )
        for value, allowed, label in checks:
            if value not in allowed:
                raise ValueError(f"非法 {label}: {value}")


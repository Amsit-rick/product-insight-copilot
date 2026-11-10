"""诊断任务服务：创建（幂等）、查询、取消、重试与事件流。"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_utils import get_logger
from app.models.enums import TaskStatus
from app.models.identity import User
from app.models.workflow import AgentRun, AnalysisEvent, AnalysisTask, Report
from app.services.audit import record_audit

logger = get_logger(__name__)


class TaskNotFound(Exception):
    pass


class TaskConflict(Exception):
    """状态不允许该操作（例如已完成的任务被取消）。"""


class TaskRevisionConflict(TaskConflict):
    """调用方基于旧任务版本写入，必须刷新后重试。"""

    def __init__(self, *, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            f"任务版本冲突：期望 {expected_revision}，实际 {actual_revision}，请刷新后重试"
        )

    def as_detail(self) -> dict:
        return {
            "code": "task_revision_conflict",
            "message": str(self),
            "expected_revision": self.expected_revision,
            "actual_revision": self.actual_revision,
        }


class TaskService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    # ------------------------------------------------------------------ 创建
    def create(
        self,
        *,
        user: User,
        query: str,
        filters: dict | None = None,
        title: str | None = None,
        idempotency_key: str | None = None,
        force_human_review: bool = False,
    ) -> tuple[AnalysisTask, bool]:
        """创建任务。返回 (task, created)；命中幂等键时 created=False。"""
        if idempotency_key:
            existing = (
                self.session.query(AnalysisTask)
                .filter(AnalysisTask.idempotency_key == idempotency_key)
                .filter(AnalysisTask.user_id == user.id)
                .first()
            )
            if existing is not None:
                logger.info("幂等命中，复用任务 task_id=%s", existing.id)
                from app.services.task_execution import TaskExecutionService

                TaskExecutionService(self.session).ensure_root(existing)
                return existing, False

        task = AnalysisTask(
            tenant_id=user.tenant_id,
            user_id=user.id,
            title=(title or query[:60]),
            query=query,
            filters=filters or {},
            status=TaskStatus.PENDING.value,
            workflow_version=self.settings.workflow_version,
            prompt_version=self.settings.prompt_version,
            idempotency_key=idempotency_key,
            cancel_requested=False,
        )
        self.session.add(task)
        self.session.flush()
        from app.services.task_execution import TaskExecutionService

        TaskExecutionService(self.session).ensure_root(task)
        self.session.add(
            AnalysisEvent(
                task_id=task.id,
                node="create_task",
                level="info",
                message="任务已创建，等待执行",
                payload={"intent_hint": None, "filters": filters or {}},
            )
        )
        record_audit(
            self.session,
            action="task_created",
            tenant_id=user.tenant_id,
            actor_id=user.id,
            actor_name=user.username,
            resource_type="analysis_task",
            resource_id=task.id,
            detail={
                "query": query[:500],
                "filters": filters or {},
                "force_human_review": force_human_review,
                "idempotency_key": idempotency_key,
            },
        )
        if force_human_review:
            task.filters = {**(filters or {}), "force_human_review": True}
        self.session.flush()
        return task, True

    # ------------------------------------------------------------------ 查询
    def get(self, task_id: str, *, tenant_id: str | None = None) -> AnalysisTask:
        task = self.session.get(AnalysisTask, task_id)
        if task is None or (tenant_id and task.tenant_id != tenant_id):
            raise TaskNotFound(f"任务不存在: {task_id}")
        return task

    def list(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        user_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
    ) -> tuple[list[AnalysisTask], int]:
        query = self.session.query(AnalysisTask).filter(AnalysisTask.tenant_id == tenant_id)
        if status:
            query = query.filter(AnalysisTask.status == status)
        if user_id:
            query = query.filter(AnalysisTask.user_id == user_id)
        if keyword:
            query = query.filter(AnalysisTask.query.ilike(f"%{keyword}%"))
        total = query.count()
        items = (
            query.order_by(AnalysisTask.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, int(total)

    def events(self, task_id: str, *, after: str | None = None, limit: int = 200) -> list[AnalysisEvent]:
        query = self.session.query(AnalysisEvent).filter(AnalysisEvent.task_id == task_id)
        if after:
            anchor = self.session.get(AnalysisEvent, after)
            if anchor is not None:
                query = query.filter(AnalysisEvent.created_at >= anchor.created_at)
        return query.order_by(AnalysisEvent.created_at.asc()).limit(limit).all()

    def agent_runs(self, task_id: str) -> list[AgentRun]:
        return (
            self.session.query(AgentRun)
            .filter(AgentRun.task_id == task_id)
            .order_by(AgentRun.created_at.asc())
            .all()
        )

    # ------------------------------------------------------------------ 状态变更
    def claim_revision(
        self,
        task_id: str,
        *,
        tenant_id: str | None = None,
        expected_revision: int | None = None,
        allowed_statuses: set[str] | None = None,
        invalid_status_message: str | None = None,
    ) -> AnalysisTask:
        """原子占用任务版本，并返回 revision 已加一的最新任务。

        即使旧客户端没有传 expected_revision，也使用读取到的当前版本做 CAS，
        因而两个并发旧请求仍只有一个能成功。
        """
        current = self.get(task_id, tenant_id=tenant_id)
        actual_revision = int(current.state_revision or 0)
        if expected_revision is not None and expected_revision != actual_revision:
            raise TaskRevisionConflict(
                expected_revision=expected_revision,
                actual_revision=actual_revision,
            )
        if allowed_statuses is not None and current.status not in allowed_statuses:
            raise TaskConflict(
                invalid_status_message or f"任务当前状态为 {current.status}，不允许该操作"
            )

        query = self.session.query(AnalysisTask).filter(AnalysisTask.id == task_id)
        if tenant_id:
            query = query.filter(AnalysisTask.tenant_id == tenant_id)
        query = query.filter(AnalysisTask.state_revision == actual_revision)
        if allowed_statuses is not None:
            query = query.filter(AnalysisTask.status.in_(allowed_statuses))
        updated = query.update(
            {AnalysisTask.state_revision: actual_revision + 1},
            synchronize_session=False,
        )
        if updated != 1:
            self.session.expire_all()
            latest = self.get(task_id, tenant_id=tenant_id)
            latest_revision = int(latest.state_revision or 0)
            if allowed_statuses is not None and latest.status not in allowed_statuses:
                raise TaskConflict(
                    invalid_status_message
                    or f"任务当前状态为 {latest.status}，不允许该操作"
                )
            raise TaskRevisionConflict(
                expected_revision=(
                    expected_revision if expected_revision is not None else actual_revision
                ),
                actual_revision=latest_revision,
            )
        self.session.flush()
        self.session.expire_all()
        return self.get(task_id, tenant_id=tenant_id)

    def request_cancel(
        self,
        task_id: str,
        *,
        tenant_id: str | None = None,
        actor: User | None = None,
        expected_revision: int | None = None,
    ) -> AnalysisTask:
        task = self.claim_revision(
            task_id,
            tenant_id=tenant_id,
            expected_revision=expected_revision,
            allowed_statuses={
                TaskStatus.PENDING.value,
                TaskStatus.RUNNING.value,
                TaskStatus.WAITING_HUMAN.value,
            },
            invalid_status_message="任务已结束，无需取消",
        )
        task.cancel_requested = True
        from app.services.task_execution import TaskExecutionService

        TaskExecutionService(self.session).request_cancel_descendants(
            task.id,
            reason="parent_cancelled",
        )
        self.session.add(
            AnalysisEvent(
                task_id=task.id,
                node="cancel",
                level="warn",
                message="收到取消请求，将在当前节点边界生效",
                payload={"actor": actor.username if actor else None},
            )
        )
        record_audit(
            self.session,
            action="task_cancel_requested",
            tenant_id=task.tenant_id,
            actor_id=actor.id if actor else None,
            resource_type="analysis_task",
            resource_id=task.id,
        )
        self.session.flush()
        return task

    def mark_cancelled(self, task_id: str) -> None:
        from app.models.base import utcnow

        task = self.session.get(AnalysisTask, task_id)
        if task is None:
            return
        task.status = TaskStatus.CANCELLED.value
        task.finished_at = utcnow()
        task.waiting_reason = None
        from app.services.task_runtime import advance_task_runtime

        advance_task_runtime(task)
        from app.services.task_execution import TaskExecutionService

        TaskExecutionService(self.session).finalize_cancel_descendants(task.id)
        self.session.flush()

    def prepare_retry(
        self,
        task_id: str,
        *,
        tenant_id: str | None = None,
        reset_retry_count: bool = True,
        actor: User | None = None,
        reason: str | None = None,
        expected_revision: int | None = None,
    ) -> AnalysisTask:
        """把任务放回可执行状态：清空取消标记，累计重试次数。"""
        task = self.claim_revision(
            task_id,
            tenant_id=tenant_id,
            expected_revision=expected_revision,
            allowed_statuses={
                TaskStatus.PENDING.value,
                TaskStatus.WAITING_HUMAN.value,
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
            },
            invalid_status_message="任务正在执行，无法重试",
        )
        task.cancel_requested = False
        task.status = TaskStatus.PENDING.value
        task.error = None
        task.waiting_reason = None
        task.wake_reason = "manual_retry"
        task.resumed_from_checkpoint = False
        task.retry_count = 0 if reset_retry_count else (task.retry_count or 0) + 1
        from app.services.task_execution import TaskExecutionService

        TaskExecutionService(self.session).sync_root(task)
        self.session.add(
            AnalysisEvent(
                task_id=task.id,
                node="retry",
                level="warn",
                message=f"任务重试（第 {task.retry_count + 1} 次）"
                + (f"：{reason}" if reason else ""),
                payload={"reset_retry_count": reset_retry_count},
            )
        )
        record_audit(
            self.session,
            action="task_retried",
            tenant_id=task.tenant_id,
            actor_id=actor.id if actor else None,
            resource_type="analysis_task",
            resource_id=task.id,
            detail={"reason": reason, "retry_count": task.retry_count},
        )
        self.session.flush()
        return task

    # ------------------------------------------------------------------ 输出
    def to_out(self, task: AnalysisTask) -> dict:
        result = task.result or {}
        return {
            "id": task.id,
            "tenant_id": task.tenant_id,
            "user_id": task.user_id,
            "title": task.title,
            "query": task.query,
            "status": task.status,
            "current_node": task.current_node,
            "current_phase": task.current_phase,
            "checkpoint_version": task.checkpoint_version or 0,
            "phase_attempt": task.phase_attempt or 0,
            "resume_count": task.resume_count or 0,
            "resumed_from_checkpoint": bool(task.resumed_from_checkpoint),
            "wake_reason": task.wake_reason,
            "state_revision": task.state_revision or 0,
            "intent": task.intent,
            "plan": task.plan or [],
            "filters": task.filters or {},
            "risk_level": task.risk_level,
            "retry_count": task.retry_count or 0,
            "workflow_version": task.workflow_version,
            "prompt_version": task.prompt_version,
            "waiting_reason": task.waiting_reason,
            "idempotency_key": task.idempotency_key,
            "evidence_count": int(result.get("evidence_count") or 0),
            "report_id": result.get("report_id"),
            "error": task.error,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    # ------------------------------------------------------------------ 统计
    def status_counts(self, tenant_id: str) -> dict:
        rows = (
            self.session.query(AnalysisTask.status, func.count(AnalysisTask.id))
            .filter(AnalysisTask.tenant_id == tenant_id)
            .group_by(AnalysisTask.status)
            .all()
        )
        counts = {status.value: 0 for status in TaskStatus}
        for status, value in rows:
            counts[status] = int(value)
        return counts

    def latest_report(self, task_id: str) -> Report | None:
        return (
            self.session.query(Report)
            .filter(Report.task_id == task_id)
            .order_by(Report.version.desc())
            .first()
        )


def reconcile_stale_tasks(
    session: Session, *, reason: str = "服务重启导致执行中断"
) -> list[str]:
    """服务启动时清理"进程中断遗留"的任务。

    为什么要这一步：工作流跑在**进程内线程池**里，进程一旦退出，正在执行的任务
    既不会再有人推进，也不会自己变成失败——它会永远挂在 running，
    前端上看就是"一直执行中"，用户既看不到错，也不知道能重试。

    因此启动时把所有 running/pending 的任务判定为中断失败并留下事件与审计。
    前提是单实例部署（见 README 已知限制）；多副本部署需要改成心跳检测。
    """
    from app.models.base import utcnow

    stale = (
        session.query(AnalysisTask)
        .filter(AnalysisTask.status.in_([TaskStatus.RUNNING.value, TaskStatus.PENDING.value]))
        .all()
    )
    interrupted: list[str] = []
    for task in stale:
        task.status = TaskStatus.FAILED.value
        task.error = {"code": "interrupted", "message": f"{reason}，可直接重试"}
        task.waiting_reason = None
        task.finished_at = utcnow()
        session.add(
            AnalysisEvent(
                task_id=task.id,
                node="startup_reconcile",
                level="warn",
                message=f"{reason}：任务被标记为失败（原始节点：{task.current_node or '未知'}），可直接重试",
                payload={"previous_status": TaskStatus.RUNNING.value, "current_node": task.current_node},
            )
        )
        record_audit(
            session,
            action="task_interrupted_on_startup",
            tenant_id=task.tenant_id,
            resource_type="analysis_task",
            resource_id=task.id,
            outcome="failed",
            detail={"reason": reason, "current_node": task.current_node},
        )
        interrupted.append(task.id)
    session.flush()
    return interrupted


def date_range(filters: dict) -> tuple[date | None, date | None]:
    """从 filters 解析时间范围（供 API 与评测复用）。"""

    def _parse(value):
        if not value:
            return None
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except Exception:
            return None

    return _parse(filters.get("date_from")), _parse(filters.get("date_to"))

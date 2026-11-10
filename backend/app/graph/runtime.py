"""节点运行时：把会话、事件、取消检查、审计收敛到一处。

节点函数保持"纯"逻辑，所有副作用都通过 `NodeRuntime` 完成：
- 每个节点用自己的短生命周期 Session（工作流跑在后台线程，Session 不能跨线程共享）；
- 关键节点写 `analysis_events`，前端进度页与排障都依赖它；
- 每个节点入口检查取消标记，取消能在一个节点边界内生效；
- 审计日志（模型调用、证据引用、人工修改、审批）统一走 AuditService。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import SessionLocal, write_scope
from app.logging_utils import get_logger
from app.models.base import utcnow
from app.models.enums import EventLevel, NodeStatus, TaskStatus
from app.models.workflow import AnalysisEvent, AnalysisTask
from app.services.audit import record_audit
from app.services.task_runtime import advance_task_runtime

logger = get_logger(__name__)


class TaskCancelled(Exception):
    """用户取消：在节点边界抛出，由 runner 收敛为 cancelled 状态。"""


@dataclass
class NodeContext:
    """一次节点执行的上下文。

    `short_writes` 决定"记账型写入"（事件、审计）走哪条路，这一点很关键：

    - `False`（默认，短节点）：写入走**当前节点会话**，与节点自身的业务写入同一个事务，
      顺序一致、不会出现"事件已提交但业务回滚"的错位；
    - `True`（跨长时间 IO 的节点：Agent 节点、补检索节点）：写入走**短写事务**，
      因为这些节点会把会话打开数秒（模型调用），若在其中写库会把写锁一直握着，
      SQLite 下并行 Agent 直接互相锁死。

    注意：两者不能混用——同一线程里"节点会话有未提交写入 + 另开连接写库"会自锁。
    """

    runtime: "NodeRuntime"
    session: Session
    state: dict
    node: str
    task: AnalysisTask | None = None
    short_writes: bool = False

    # ------------------------------------------------------------------ 日志
    def emit(
        self,
        message: str,
        payload: dict | None = None,
        *,
        level: EventLevel | str = EventLevel.INFO,
    ) -> None:
        level_value = level.value if isinstance(level, EventLevel) else str(level)
        if self.short_writes:
            self.runtime.emit_event(
                self.state.get("task_id", ""), self.node, message, payload or {}, level=level_value
            )
            return
        self.session.add(
            AnalysisEvent(
                task_id=self.state.get("task_id", ""),
                node=self.node,
                level=level_value,
                message=message[:2000],
                payload=payload or {},
            )
        )
        self.session.flush()

    # ------------------------------------------------------------------ 状态
    def check_cancel(self) -> None:
        if self.task is None:
            return
        self.session.refresh(self.task)
        if self.task.cancel_requested:
            raise TaskCancelled(f"任务 {self.task.id} 已被取消")

    def _apply_status(
        self,
        *,
        status: TaskStatus | None = None,
        current_node: str | None = None,
        waiting_reason: str | None = None,
    ) -> bool:
        """在当前节点会话里改任务状态并**立即提交**。

        为什么不用短写事务：非短写节点（如人工审批节点）会先往节点会话里写事件/审批记录，
        此时节点会话已持有 SQLite 写锁；再开第二个连接改 `analysis_tasks` 就是自己锁自己，
        表现为 `database is locked` 或长时间等待。这里复用同一连接并立刻提交，既避免自锁，
        又能立刻释放写锁（不会把锁拖到人工等待期间）。

        返回 True 表示已处理；False 表示当前没有加载任务对象，调用方应回退到短写事务。
        """
        task = self.task
        if task is None:
            return False
        if status is not None:
            task.status = status.value
            if status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                task.finished_at = utcnow()
            if status == TaskStatus.RUNNING and task.started_at is None:
                task.started_at = utcnow()
        if current_node is not None:
            advance_task_runtime(task, current_node=current_node)
        elif status is not None or waiting_reason is not None:
            advance_task_runtime(task)
        if waiting_reason is not None:
            task.waiting_reason = waiting_reason[:255]
        from app.services.task_execution import TaskExecutionService

        TaskExecutionService(self.session).sync_root(task)
        self.session.commit()
        return True

    def touch(self, *, current_node: bool = True, status: TaskStatus | None = None) -> None:
        """推进节点/状态：优先复用节点会话并立即提交，否则走短写事务。"""
        task_id = self.state.get("task_id")
        if not task_id:
            return
        node = self.node if current_node else None
        if self._apply_status(status=status, current_node=node):
            return
        self.runtime.mark_task(task_id, status=status, current_node=node)

    def wait_for_human(self, reason: str) -> None:
        task_id = self.state.get("task_id")
        if not task_id:
            return
        if self._apply_status(status=TaskStatus.WAITING_HUMAN, waiting_reason=reason):
            return
        self.runtime.mark_task(
            task_id,
            status=TaskStatus.WAITING_HUMAN,
            current_node=self.node,
            waiting_reason=reason,
        )

    def resume_from_human(self) -> None:
        task_id = self.state.get("task_id")
        if not task_id:
            return
        if self._apply_status(status=TaskStatus.RUNNING, waiting_reason=""):
            return
        self.runtime.mark_task(
            task_id,
            status=TaskStatus.RUNNING,
            waiting_reason="",
        )

    def audit(self, action: str, **kwargs: Any) -> None:
        if self.short_writes:
            with write_scope() as session:
                record_audit(
                    session,
                    action=action,
                    tenant_id=self.state.get("tenant_id", "*"),
                    actor_id=self.state.get("user_id"),
                    resource_type="analysis_task",
                    resource_id=self.state.get("task_id"),
                    **kwargs,
                )
            return
        record_audit(
            self.session,
            action=action,
            tenant_id=self.state.get("tenant_id", "*"),
            actor_id=self.state.get("user_id"),
            resource_type="analysis_task",
            resource_id=self.state.get("task_id"),
            **kwargs,
        )


class NodeRuntime:
    """工作流运行时依赖注入点（测试时可以替换 Session 工厂）。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory or SessionLocal
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------ 会话
    @contextmanager
    def node_session(
        self,
        state: dict,
        node: str,
        *,
        load_task: bool = True,
        status: TaskStatus | None = None,
        short_writes: bool = False,
    ) -> Iterator[NodeContext]:
        """节点会话。

        `short_writes=True` 用于**跨长时间 IO 的节点**（Agent、补检索）：这些节点把会话
        打开数秒，任何写操作都必须走短写事务，否则会把 SQLite 写锁握住整个模型调用时长。
        这类节点里 **不要**用 `ctx.session` 写业务数据（会与短写事务互相锁死）。
        """
        session = self.session_factory()
        task = None
        if load_task and state.get("task_id"):
            task = session.get(AnalysisTask, state["task_id"])
        ctx = NodeContext(
            runtime=self,
            session=session,
            state=state,
            node=node,
            task=task,
            short_writes=short_writes,
        )
        try:
            if task is not None:
                ctx.touch(status=status)
            elif state.get("task_id"):
                # 证据合并/反思等轻量节点不持有任务行，但阶段仍必须对外可见。
                self.mark_task(
                    state["task_id"],
                    status=status,
                    current_node=node,
                )
            yield ctx
            session.commit()
        except TaskCancelled:
            session.rollback()
            raise
        except Exception as exc:
            session.rollback()
            self.record_failure(state, node, exc)
            raise
        finally:
            session.close()

    def record_failure(self, state: dict, node: str, exc: Exception) -> None:
        """失败事件用独立会话写入，避免被回滚吞掉。"""
        try:
            session = self.session_factory()
            try:
                session.add(
                    AnalysisEvent(
                        task_id=state.get("task_id", ""),
                        node=node,
                        level=EventLevel.ERROR.value,
                        message=f"节点执行失败：{type(exc).__name__}: {exc}"[:2000],
                        payload={"node": node},
                    )
                )
                task = session.get(AnalysisTask, state.get("task_id", ""))
                if task is not None:
                    advance_task_runtime(task, current_node=node)
                session.commit()
            finally:
                session.close()
        except Exception as inner:  # pragma: no cover - 记录失败不应再抛异常
            logger.error("写入失败事件时出错: %s", inner)

    # ------------------------------------------------------------------ 工具
    def mark_task(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        current_node: str | None = None,
        error: dict | None = None,
        waiting_reason: str | None = None,
        result: dict | None = None,
    ) -> None:
        session = self.session_factory()
        try:
            task = session.get(AnalysisTask, task_id)
            if task is None:
                return
            if status is not None:
                task.status = status.value
                if status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                    task.finished_at = utcnow()
                if status == TaskStatus.RUNNING and task.started_at is None:
                    task.started_at = utcnow()
            if current_node is not None:
                advance_task_runtime(task, current_node=current_node)
            elif any(value is not None for value in (status, error, waiting_reason, result)):
                advance_task_runtime(task)
            if error is not None:
                task.error = error
            if waiting_reason is not None:
                task.waiting_reason = waiting_reason[:255]
            if result is not None:
                task.result = result
            from app.services.task_execution import TaskExecutionService

            TaskExecutionService(session).sync_root(task)
            session.commit()
        finally:
            session.close()

    def record_resume(
        self,
        task_id: str,
        *,
        resumed_from_checkpoint: bool,
        wake_reason: str,
        expected_revision: int | None = None,
    ) -> None:
        """Persist recovery provenance before continuing workflow execution."""
        session = self.session_factory()
        try:
            from app.services.task_service import TaskNotFound, TaskService

            try:
                task = TaskService(session).claim_revision(
                    task_id,
                    expected_revision=expected_revision,
                )
            except TaskNotFound:
                return
            task.resume_count = int(task.resume_count or 0) + 1
            task.resumed_from_checkpoint = resumed_from_checkpoint
            task.wake_reason = wake_reason[:64]
            task.status = TaskStatus.RUNNING.value
            if task.started_at is None:
                task.started_at = utcnow()
            from app.services.task_execution import TaskExecutionService

            TaskExecutionService(session).sync_root(task)
            session.commit()
        finally:
            session.close()

    def emit_event(
        self,
        task_id: str,
        node: str,
        message: str,
        payload: dict | None = None,
        *,
        level: EventLevel | str = EventLevel.INFO,
    ) -> None:
        session = self.session_factory()
        try:
            session.add(
                AnalysisEvent(
                    task_id=task_id,
                    node=node,
                    level=level.value if isinstance(level, EventLevel) else str(level),
                    message=message[:2000],
                    payload=payload or {},
                )
            )
            session.commit()
        except Exception as exc:  # pragma: no cover
            logger.warning("写入事件失败: %s", exc)
        finally:
            session.close()


def node_status_of(ok: bool, *, skipped: bool = False) -> NodeStatus:
    if skipped:
        return NodeStatus.SKIPPED
    return NodeStatus.SUCCEEDED if ok else NodeStatus.FAILED

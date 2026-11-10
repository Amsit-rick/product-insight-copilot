"""工作流执行器：后台线程执行、中断等待、断点恢复与失败收敛。

为什么要自己管线程：工作流是阻塞 IO（检索、模型、数据库），
放在 FastAPI 的请求线程里会拖垮接口；放在独立进程又要引入队列与序列化成本。
第一版采用**进程内线程池 + LangGraph checkpointer**：

- 任务 ID 即 thread_id，中断（人工审核）后可以用同一 checkpointer 恢复；
- 进程重启导致 checkpoint 丢失时，走"降级恢复"路径：用落库的结论摘要
  （`AnalysisTask.result.state_digest`）直接重建报告，而不是让审核结论白填；
- 失败不会污染任务表：状态、错误码与事件都写清楚，可重试。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.config import Settings, get_settings
from app.db.session import SessionLocal
from app.graph import build_graph, initial_state
from app.graph.runtime import NodeRuntime, TaskCancelled
from app.logging_utils import get_logger
from app.models.enums import EventLevel, TaskStatus
from app.models.workflow import AnalysisTask
from app.services.task_service import TaskRevisionConflict, TaskService

logger = get_logger(__name__)


class WorkflowRunner:
    """应用内的工作流执行器（单例由 `get_runner()` 提供）。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] | None = None,
        runtime: NodeRuntime | None = None,
        checkpointer: Any | None = None,
        settings: Settings | None = None,
        max_workers: int | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory or SessionLocal
        self.runtime = runtime or NodeRuntime(
            session_factory=self.session_factory, settings=self.settings
        )
        # checkpointer 按配置构建（memory / sqlite / postgres）：
        # 它决定"人工审核后的恢复"能不能跨进程、跨副本，不能写死成进程内内存
        if checkpointer is None:
            from app.graph.checkpointer import build_checkpointer

            checkpointer = build_checkpointer(self.settings)
        self.checkpointer = checkpointer
        self._executor: ThreadPoolExecutor | None = None
        self._max_workers = max_workers or self.settings.task_worker_threads
        self._lock = threading.Lock()
        self._futures: dict[str, Future] = {}
        self._graph = build_graph(self.runtime, checkpointer=self.checkpointer)
        # 按工作流名缓存已编译的图：诊断（默认）与售后工单两套拓扑共用同一个运行器、
        # checkpointer 与事件/成本/审计设施。
        self._graphs: dict[str, Any] = {"diagnosis": self._graph}

    # ------------------------------------------------------------------ 工作流选择
    def _graph_for(self, workflow: str | None):
        """按名称取图（未知名退回诊断图，保证旧任务仍可恢复执行）。"""
        name = (workflow or "diagnosis").strip() or "diagnosis"
        if name in self._graphs:
            return self._graphs[name]
        if name == "after_sales":
            from app.graph.after_sales import build_after_sales_graph

            self._graphs[name] = build_after_sales_graph(
                self.runtime, checkpointer=self.checkpointer
            )
        else:
            self._graphs[name] = self._graph
        return self._graphs[name]

    # ------------------------------------------------------------------ 执行器
    @property
    def executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._max_workers, thread_name_prefix="workflow"
                )
            return self._executor

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            if self._executor is not None:
                self._executor.shutdown(wait=wait)
                self._executor = None

    @property
    def graph(self):
        return self._graph

    # ------------------------------------------------------------------ 提交
    def submit(self, task_id: str) -> Future:
        future = self.executor.submit(self.run, task_id)
        self._futures[task_id] = future
        return future

    def submit_resume(self, task_id: str, decision: dict) -> Future:
        future = self.executor.submit(self.resume, task_id, decision)
        self._futures[task_id] = future
        return future

    def running_count(self) -> int:
        return len([f for f in self._futures.values() if not f.done()])

    # ------------------------------------------------------------------ 启动
    def run(self, task_id: str) -> dict:
        """同步执行一个任务（后台线程与测试都走这里）。"""
        session = self.session_factory()
        try:
            task = session.get(AnalysisTask, task_id)
            if task is None:
                raise ValueError(f"任务不存在: {task_id}")
            if task.status == TaskStatus.CANCELLED.value:
                return {"status": TaskStatus.CANCELLED.value}
            state = self._initial_state(task)
            workflow = (task.filters or {}).get("workflow")
            task.status = TaskStatus.RUNNING.value
            task.started_at = task.started_at or _utcnow()
            from app.services.task_execution import TaskExecutionService

            TaskExecutionService(session).sync_root(task)
            session.commit()
        finally:
            session.close()

        self.runtime.emit_event(
            task_id, "runner", "工作流开始执行", {"thread_id": task_id, "workflow": workflow or "diagnosis"}
        )
        return self._invoke(state, task_id, workflow=workflow)

    # ------------------------------------------------------------------ 恢复
    def _record_resume(
        self,
        task_id: str,
        *,
        resumed_from_checkpoint: bool,
        wake_reason: str,
        expected_revision: int | None,
    ) -> bool:
        try:
            self.runtime.record_resume(
                task_id,
                resumed_from_checkpoint=resumed_from_checkpoint,
                wake_reason=wake_reason,
                expected_revision=expected_revision,
            )
            return True
        except TaskRevisionConflict as exc:
            self.runtime.emit_event(
                task_id,
                "runner",
                "恢复请求已过期，已安全跳过",
                {
                    "expected_revision": exc.expected_revision,
                    "actual_revision": exc.actual_revision,
                },
                level=EventLevel.WARN,
            )
            return False

    def resume(self, task_id: str, decision: dict) -> dict:
        """恢复被人工审核中断的任务。"""
        decision = dict(decision)
        expected_revision = decision.pop("_expected_revision", None)
        config = {"configurable": {"thread_id": task_id}}
        # 先取任务以确定用哪张图：售后工单与诊断的拓扑不同，
        # 用错图会拿旧 checkpoint 去跑另一套节点（表现为莫名其妙的 KeyError）。
        workflow = None
        session = self.session_factory()
        try:
            task = session.get(AnalysisTask, task_id)
            if task is not None:
                workflow = (task.filters or {}).get("workflow")
        finally:
            session.close()
        graph = self._graph_for(workflow)

        snapshot = None
        try:
            snapshot = graph.get_state(config)
        except Exception as exc:  # pragma: no cover - checkpointer 异常时走降级恢复
            logger.warning("读取 checkpoint 失败: %s", exc)

        if snapshot is not None and snapshot.next:
            if not self._record_resume(
                task_id,
                resumed_from_checkpoint=True,
                wake_reason="human_approval",
                expected_revision=expected_revision,
            ):
                return {"status": "revision_conflict", "task_id": task_id}
            self.runtime.emit_event(
                task_id, "runner", "人工审核完成，恢复工作流", {"next": list(snapshot.next)}
            )
            try:
                result = graph.invoke(Command(resume=decision), config)
                return self._post_process(task_id, dict(result))
            except Exception as exc:
                self._fail(task_id, exc)
                return {"status": TaskStatus.FAILED.value, "error": str(exc)}

        # 降级恢复：进程重启后 checkpoint 可能已丢失
        self.runtime.emit_event(
            task_id,
            "runner",
            "未找到可恢复的 checkpoint，改用落库摘要重建报告",
            level=EventLevel.WARN,
        )
        if not self._record_resume(
            task_id,
            resumed_from_checkpoint=False,
            wake_reason="checkpoint_missing_fallback",
            expected_revision=expected_revision,
        ):
            return {"status": "revision_conflict", "task_id": task_id}
        return self._resume_from_digest(task_id, decision)

    # ------------------------------------------------------------------ 内部
    def _invoke(self, state: dict, task_id: str, *, workflow: str | None = None) -> dict:
        config = {"configurable": {"thread_id": task_id}}
        graph = self._graph_for(workflow or (state.get("filters") or {}).get("workflow"))
        try:
            result = graph.invoke(state, config)
            return self._post_process(task_id, dict(result))
        except TaskCancelled:
            self._cancel(task_id)
            return {"status": TaskStatus.CANCELLED.value}
        except Exception as exc:  # 失败必须落状态与事件，不能静默
            self._fail(task_id, exc)
            return {"status": TaskStatus.FAILED.value, "error": str(exc)}

    def _post_process(self, task_id: str, result: dict) -> dict:
        interrupted = _interrupt_payload(result)
        session = self.session_factory()
        try:
            task = session.get(AnalysisTask, task_id)
            if task is None:
                return result
            if interrupted is not None:
                task.status = TaskStatus.WAITING_HUMAN.value
                task.current_node = "human_approval"
                from app.services.task_execution import TaskExecutionService

                TaskExecutionService(session).sync_root(task)
                session.commit()
                self.runtime.emit_event(
                    task_id,
                    "runner",
                    "工作流已中断，等待人工审核",
                    {"reason": interrupted.get("reason")},
                    level=EventLevel.WARN,
                )
                return {"status": TaskStatus.WAITING_HUMAN.value, "interrupt": interrupted}

            if result.get("halt"):
                errors = result.get("errors") or []
                code = (errors[0].get("code") if errors else None) or "validation_failed"
                task.status = TaskStatus.FAILED.value
                task.error = {
                    "code": code,
                    "message": result.get("halt_reason") or "任务校验未通过",
                }
                task.finished_at = task.finished_at or _utcnow()
                from app.services.task_execution import TaskExecutionService

                TaskExecutionService(session).sync_root(task)
                session.commit()
                self.runtime.emit_event(
                    task_id, "runner", f"任务终止：{result.get('halt_reason')}", level=EventLevel.ERROR
                )
                self._sync_ticket_failure(task_id, result.get("halt_reason") or "任务校验未通过")
                return {"status": TaskStatus.FAILED.value, "error": task.error}

            if task.status not in {TaskStatus.CANCELLED.value}:
                task.status = TaskStatus.COMPLETED.value
                task.finished_at = task.finished_at or _utcnow()
                from app.services.task_execution import TaskExecutionService

                TaskExecutionService(session).sync_root(task)
                session.commit()
            report_id = (task.result or {}).get("report_id")
            self.runtime.emit_event(
                task_id,
                "runner",
                f"工作流执行完成（report_id={report_id}）",
                {"node_visits": result.get("node_visits") or {}},
            )
            return {
                "status": task.status,
                "report_id": report_id,
                "risk_level": result.get("risk_level"),
                "branch": result.get("branch"),
            }
        finally:
            session.close()

    def _fail(self, task_id: str, exc: Exception) -> None:
        from app.agents.base import BudgetExceeded

        # 预算超限是可解释的业务失败（不是程序缺陷）：单独给错误码，
        # 前端据此提示"提高预算或换便宜模型后重试"。
        code = "budget_exceeded" if isinstance(exc, BudgetExceeded) else type(exc).__name__
        session = self.session_factory()
        try:
            task = session.get(AnalysisTask, task_id)
            if task is None:
                return
            task.status = TaskStatus.FAILED.value
            task.error = {"code": code, "message": str(exc)[:2000]}
            task.finished_at = _utcnow()
            from app.services.task_execution import TaskExecutionService

            TaskExecutionService(session).sync_root(task)
            session.commit()
        finally:
            session.close()
        self.runtime.emit_event(
            task_id,
            "runner",
            f"工作流执行失败（{code}）：{exc}",
            level=EventLevel.ERROR,
        )
        self._sync_ticket_failure(task_id, str(exc))

    def _sync_ticket_failure(self, task_id: str, error: str) -> None:
        self._sync_ticket_outcome(task_id, TaskStatus.FAILED.value, error)

    def _sync_ticket_outcome(self, task_id: str, outcome: str, error: str | None = None) -> None:
        """把运行结局回写到工单状态（仅售后工单任务）。

        放在运行器里而不是各节点里，是因为失败可能发生在**任意**节点上：
        节点自己处理失败必然漏（漏掉的那次就是"工单永久卡在分析中"）。
        诊断任务没有 `ticket_id`，这里直接跳过，互不影响。
        """
        session = self.session_factory()
        try:
            task = session.get(AnalysisTask, task_id)
            ticket_id = ((task.filters or {}) if task is not None else {}).get("ticket_id")
            if not ticket_id:
                return
            from app.services.ticket_service import TicketService

            TicketService(session).sync_analysis_aborted(
                ticket_id=ticket_id, run_id=task_id, outcome=outcome, error=error
            )
        except Exception as sync_exc:  # 回写失败不能掩盖原始失败
            logger.warning("回写工单状态失败: %s", sync_exc)
        finally:
            session.close()

    def _cancel(self, task_id: str) -> None:
        session = self.session_factory()
        try:
            TaskService(session).mark_cancelled(task_id)
            session.commit()
        finally:
            session.close()
        self.runtime.emit_event(task_id, "runner", "任务已取消", level=EventLevel.WARN)
        self._sync_ticket_outcome(task_id, TaskStatus.CANCELLED.value, "运行被取消")

    def _initial_state(self, task: AnalysisTask) -> dict:
        """从任务行构造工作流初始状态。"""
        return dict(
            initial_state(
                task_id=task.id,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                query=task.query,
                filters=dict(task.filters or {}),
                kb_codes=list((task.filters or {}).get("knowledge_bases") or []),
                max_security_rank=1,  # 真实密级在 validate_request 里按用户重新计算
                force_human_review=bool((task.filters or {}).get("force_human_review")),
                workflow_version=task.workflow_version or self.settings.workflow_version,
                prompt_version=task.prompt_version or self.settings.prompt_version,
            )
        )

    # ------------------------------------------------------------------ 降级恢复
    def _resume_from_digest(self, task_id: str, decision: dict) -> dict:
        from app.graph.nodes.report import (
            apply_post_report_approval,
            generate_report,
            persist_result,
            reflect_report,
            revise_report_node,
            validate_report,
        )
        from app.services.approval_service import apply_decision, approval_status_for

        session = self.session_factory()
        try:
            task = session.get(AnalysisTask, task_id)
            if task is None:
                return {"status": TaskStatus.FAILED.value, "error": "任务不存在"}
            digest = (task.result or {}).get("state_digest")
            if not digest:
                return {
                    "status": TaskStatus.FAILED.value,
                    "error": "缺少结论摘要，无法恢复；请重新发起诊断",
                }
            apply_decision(
                session,
                task_id=task_id,
                action=decision.get("action", "approve"),
                reviewer_id=decision.get("reviewer_id"),
                comment=decision.get("comment"),
                modifications=decision.get("modifications"),
            )
            task.status = TaskStatus.RUNNING.value
            session.commit()
        finally:
            session.close()

        state = dict(
            initial_state(
                task_id=task_id,
                tenant_id=digest.get("tenant_id") or "*",
                user_id=digest.get("user_id") or "",
                query=digest.get("query") or "",
                filters=digest.get("filters") or {},
            )
        )
        state.update(
            {
                "intent": digest.get("intent"),
                "risk_level": digest.get("risk_level") or "high",
                "risk_reasons": digest.get("risk_reasons") or [],
                "evidence": digest.get("evidence") or [],
                "metric_results": digest.get("metric_results") or [],
                "statements": digest.get("statements") or [],
                "conflicts": digest.get("conflicts") or [],
                "coverage": digest.get("coverage") or {},
                "agent_results": digest.get("agent_results") or {},
                "warnings": digest.get("warnings") or [],
                "sufficiency": digest.get("sufficiency") or {"sufficient": True},
                "approval_status": approval_status_for(decision.get("action")),
                "approval": {
                    "action": decision.get("action", "approve"),
                    "reviewer_id": decision.get("reviewer_id"),
                    "comment": decision.get("comment"),
                    "modifications": decision.get("modifications") or {},
                },
                "needs_human_review": True,
                "report_draft": digest.get("report_draft"),
                "reflection_rounds": int(digest.get("reflection_rounds") or 0),
                "max_reflection_rounds": int(digest.get("max_reflection_rounds") or 1),
                "reflection_history": digest.get("reflection_history") or {},
                "latest_reflection": digest.get("latest_reflection"),
                "draft_version": int(digest.get("draft_version") or 0),
                "draft_kind": digest.get("draft_kind"),
                "revision_history": digest.get("revision_history") or {},
                "unresolved_reflection_issues": digest.get("unresolved_reflection_issues") or [],
                "reflection_stop_reason": digest.get("reflection_stop_reason"),
                "protected_decision_snapshot": digest.get("protected_decision_snapshot"),
                "protected_decision_hash": digest.get("protected_decision_hash"),
                "decision_version": int(digest.get("decision_version") or 0),
                "review_stage": digest.get("review_stage"),
                "reflection_idempotency_key": digest.get("reflection_idempotency_key"),
            }
        )
        if state.get("review_stage") == "post_report_reflection" and state.get("report_draft"):
            state.update(apply_post_report_approval(state, runtime=self.runtime))
            nodes = (validate_report, persist_result)
        else:
            state.update(generate_report(state, runtime=self.runtime))
            if (state.get("filters") or {}).get("report_reflection_enabled"):
                state.update(reflect_report(state, runtime=self.runtime))
                if (state.get("latest_reflection") or {}).get("verdict") == "revise":
                    state.update(revise_report_node(state, runtime=self.runtime))
                    state.update(reflect_report(state, runtime=self.runtime))
                if (state.get("latest_reflection") or {}).get("verdict") == "human_review":
                    state["needs_human_review"] = True
                    state["reflection_stop_reason"] = (
                        state.get("reflection_stop_reason") or "checkpoint_recovery_requires_review"
                    )
            nodes = (validate_report, persist_result)
        for node in nodes:
            state.update(node(state, runtime=self.runtime))
        self.runtime.emit_event(
            task_id,
            "runner",
            f"降级恢复完成，报告已重建（report_id={state.get('report_id')}）",
        )
        return {
            "status": TaskStatus.COMPLETED.value,
            "report_id": state.get("report_id"),
            "recovered": True,
        }


def _interrupt_payload(result: dict) -> dict | None:
    """从图输出里取出中断信息（不同版本可能放在 __interrupt__ 里）。"""
    raw = result.get("__interrupt__")
    if not raw:
        return None
    item = raw[0] if isinstance(raw, (list, tuple)) else raw
    value = getattr(item, "value", None)
    if isinstance(value, dict):
        return value
    if isinstance(item, dict):
        return item.get("value") or item
    return {"reason": str(value) if value else "等待人工审核"}


def _utcnow():
    from app.models.base import utcnow

    return utcnow()


_runner: WorkflowRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> WorkflowRunner:
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = WorkflowRunner()
        return _runner


def reset_runner() -> None:
    """测试用：重建执行器（会同时重置 checkpointer）。"""
    global _runner
    with _runner_lock:
        if _runner is not None:
            _runner.shutdown(wait=False)
        _runner = None

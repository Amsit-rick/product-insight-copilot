"""任务级确定性历史播放包。

这里的 replay 是 playback：只读取当时已经落库的结构化状态，不重新调用模型、
检索或业务工具。重新执行属于 rerun/retry，结果不保证与历史一致。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.workflow import (
    AgentRun,
    AnalysisEvent,
    AnalysisTask,
    Approval,
    AuditLog,
    EvidenceCitation,
    Report,
    TaskExecutionUnit,
    TaskReplaySnapshot,
    ToolCall,
)
from app.services.audit import record_audit
from app.services.masking import mask_structure
from app.services.model_gateway import sanitize_for_log
from app.services.task_service import TaskNotFound

SCHEMA_VERSION = "task-replay.v1"
BLOCKED_REASONING_KEYS = frozenset(
    {"analysis", "chain_of_thought", "reasoning", "thought", "thoughts", "scratchpad"}
)


@dataclass(frozen=True)
class ReplayVerification:
    valid: bool
    expected_digest: str
    actual_digest: str


def json_safe_value(value: Any) -> Any:
    """Return a deterministic JSON-compatible representation.

    Tool adapters and ORM-backed facts legitimately return values such as
    ``datetime`` and ``Decimal``.  Audit rows and replay snapshots use JSON
    columns, so those values must be normalized at the persistence boundary
    without flattening the surrounding structure or losing decimal precision.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return json_safe_value(value.value)
    if isinstance(value, dict):
        return {
            str(key): json_safe_value(item)
            for key, item in value.items()
            if str(key).lower() not in BLOCKED_REASONING_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [json_safe_value(item) for item in value]
    return value


def sanitize_snapshot(value: Any) -> Any:
    """移除推理字段，并对密钥、凭证与业务 PII 做递归脱敏。"""

    normalized = json_safe_value(value)
    return sanitize_for_log(mask_structure(normalized))


def canonical_digest(payload: Any) -> str:
    body = json.dumps(
        json_safe_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _row(row) -> dict:  # noqa: ANN001
    return sanitize_snapshot(row.as_dict())


def _execution_unit_row(row: TaskExecutionUnit) -> dict:
    payload = row.as_dict()
    # 数据库列名是 metadata，但 ORM 属性名必须避开 SQLAlchemy Base.metadata。
    payload["metadata"] = row.metadata_json or {}
    return payload


class TaskReplayService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _task(self, task_id: str, tenant_id: str) -> AnalysisTask:
        task = self.session.get(AnalysisTask, task_id)
        if task is None or task.tenant_id != tenant_id:
            raise TaskNotFound(f"任务不存在: {task_id}")
        return task

    def build_package(self, *, task_id: str, tenant_id: str) -> dict:
        task = self._task(task_id, tenant_id)
        # 回放包必须包含根执行单元；历史任务/旧库可能从未建过执行树。
        # 这里幂等补齐（owner_type=workflow + owner_ref_id=task_id），避免
        # "父执行单元缺失"只在特定测试顺序/旧数据上偶发。
        from app.services.task_execution import TaskExecutionService

        TaskExecutionService(self.session).ensure_root(task)
        self.session.flush()

        def ordered(model):  # noqa: ANN001
            return (
                self.session.query(model)
                .filter(model.task_id == task_id)
                .order_by(model.created_at.asc(), model.id.asc())
                .all()
            )

        audits = (
            self.session.query(AuditLog)
            .filter(AuditLog.tenant_id == tenant_id)
            .filter(AuditLog.resource_id == task_id)
            # 回放包自身的创建审计不能反过来改变回放包，否则重复生成永远无法幂等。
            .filter(AuditLog.action != "task_replay_snapshot_created")
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
            .all()
        )
        tool_calls = ordered(ToolCall)
        captured_results = sum(1 for row in tool_calls if row.result is not None)
        total_results = len(tool_calls)
        return sanitize_snapshot(
            {
                "schema_version": SCHEMA_VERSION,
                "mode": "playback",
                "reran_model_or_tools": False,
                "playback_fidelity": (
                    "complete" if captured_results == total_results else "partial"
                ),
                "completeness": {
                    "tool_results_total": total_results,
                    "tool_results_captured": captured_results,
                    "missing_tool_results": total_results - captured_results,
                },
                "task": task.as_dict(),
                "events": [row.as_dict() for row in ordered(AnalysisEvent)],
                "execution_units": [
                    _execution_unit_row(row) for row in ordered(TaskExecutionUnit)
                ],
                "agent_runs": [row.as_dict() for row in ordered(AgentRun)],
                "tool_calls": [row.as_dict() for row in tool_calls],
                "citations": [row.as_dict() for row in ordered(EvidenceCitation)],
                "approvals": [row.as_dict() for row in ordered(Approval)],
                "reports": [row.as_dict() for row in ordered(Report)],
                "audit_logs": [row.as_dict() for row in audits],
            }
        )

    def create_snapshot(
        self,
        *,
        task_id: str,
        tenant_id: str,
        created_by: str | None,
    ) -> tuple[TaskReplaySnapshot, bool]:
        package = self.build_package(task_id=task_id, tenant_id=tenant_id)
        digest = canonical_digest(package)
        existing = (
            self.session.query(TaskReplaySnapshot)
            .filter(TaskReplaySnapshot.task_id == task_id)
            .filter(TaskReplaySnapshot.digest == digest)
            .first()
        )
        if existing is not None:
            return existing, False

        snapshot = TaskReplaySnapshot(
            tenant_id=tenant_id,
            task_id=task_id,
            schema_version=SCHEMA_VERSION,
            package=package,
            digest=digest,
            created_by=created_by,
        )
        self.session.add(snapshot)
        self.session.flush()
        record_audit(
            self.session,
            action="task_replay_snapshot_created",
            tenant_id=tenant_id,
            actor_id=created_by,
            resource_type="task_replay_snapshot",
            resource_id=task_id,
            detail={"snapshot_id": snapshot.id, "digest": digest, "mode": "playback"},
        )
        self.session.commit()
        return snapshot, True

    def latest_snapshot(self, *, task_id: str, tenant_id: str) -> TaskReplaySnapshot | None:
        self._task(task_id, tenant_id)
        return (
            self.session.query(TaskReplaySnapshot)
            .filter(TaskReplaySnapshot.task_id == task_id)
            .filter(TaskReplaySnapshot.tenant_id == tenant_id)
            .order_by(TaskReplaySnapshot.created_at.desc(), TaskReplaySnapshot.id.desc())
            .first()
        )

    def verify_snapshot(self, snapshot: TaskReplaySnapshot) -> ReplayVerification:
        actual = canonical_digest(snapshot.package)
        return ReplayVerification(
            valid=actual == snapshot.digest,
            expected_digest=snapshot.digest,
            actual_digest=actual,
        )

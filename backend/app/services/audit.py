"""审计日志（设计文档 11 节）。

所有工具调用、证据引用、人工修改与审批结果都必须留痕。
审计写入失败不允许影响主流程，但会打 warning 日志。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.logging_utils import get_logger
from app.models.workflow import AuditLog
from app.services.model_gateway import sanitize_for_log

logger = get_logger(__name__)


def record_audit(
    session: Session,
    *,
    action: str,
    tenant_id: str = "*",
    actor_id: str | None = None,
    actor_name: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    outcome: str = "success",
    detail: dict | None = None,
    ip: str | None = None,
    commit: bool = False,
) -> AuditLog | None:
    """写一条审计记录。`detail` 会先做密钥/口令脱敏。"""
    try:
        row = AuditLog(
            tenant_id=tenant_id or "*",
            actor_id=actor_id,
            actor_name=actor_name,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            detail=sanitize_for_log(detail or {}),
            ip=ip,
        )
        session.add(row)
        if commit:
            session.commit()
        else:
            session.flush()
        return row
    except Exception as exc:  # pragma: no cover
        logger.warning("写入审计日志失败 action=%s: %s", action, exc)
        return None


def record_tool_audit(
    session: Session,
    *,
    task_id: str,
    agent_code: str,
    tool_name: str,
    allowed: bool,
    reason: str,
    arguments: dict[str, Any] | None = None,
) -> None:
    """策略引擎判定结果单独留痕，便于回答"为什么这次调用被拒绝"。"""
    record_audit(
        session,
        action="tool_call_decision",
        resource_type="tool",
        resource_id=tool_name,
        outcome="allow" if allowed else "deny",
        detail={
            "agent_code": agent_code,
            "task_id": task_id,
            "reason": reason,
            "arguments": arguments or {},
        },
    )

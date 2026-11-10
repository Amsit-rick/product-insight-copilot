"""持久化运行告警：供管理员确认、处理和追踪重复发生的问题。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.workflow import OperationalAlert
from app.services.audit import record_audit
from app.services.model_gateway import sanitize_for_log

ACTIVE_STATUSES = {"open", "acknowledged"}
ALLOWED_STATUSES = ACTIVE_STATUSES | {"resolved"}
ALLOWED_SEVERITIES = {"info", "warning", "critical"}


def raise_operational_alert(
    session: Session,
    *,
    tenant_id: str,
    kind: str,
    severity: str,
    title: str,
    message: str,
    fingerprint: str,
    source_type: str | None = None,
    source_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> OperationalAlert:
    """按租户与 fingerprint 合并重复告警；再次发生会重新变为待处理。"""
    if severity not in ALLOWED_SEVERITIES:
        raise ValueError(f"unsupported alert severity: {severity}")
    now = utcnow()
    row = (
        session.query(OperationalAlert)
        .filter(
            OperationalAlert.tenant_id == tenant_id,
            OperationalAlert.fingerprint == fingerprint,
        )
        .one_or_none()
    )
    if row is None:
        try:
            with session.begin_nested():
                row = OperationalAlert(
                    tenant_id=tenant_id,
                    kind=kind,
                    severity=severity,
                    title=title,
                    message=message,
                    status="open",
                    source_type=source_type,
                    source_id=source_id,
                    fingerprint=fingerprint,
                    occurrence_count=1,
                    first_occurred_at=now,
                    last_occurred_at=now,
                    metadata_json=sanitize_for_log(metadata or {}),
                )
                session.add(row)
                session.flush()
            return row
        except IntegrityError:
            row = (
                session.query(OperationalAlert)
                .filter(
                    OperationalAlert.tenant_id == tenant_id,
                    OperationalAlert.fingerprint == fingerprint,
                )
                .one()
            )

    row.kind = kind
    row.severity = severity
    row.title = title
    row.message = message
    row.status = "open"
    row.source_type = source_type
    row.source_id = source_id
    row.occurrence_count += 1
    row.last_occurred_at = now
    row.acknowledged_by = None
    row.acknowledged_at = None
    row.resolved_by = None
    row.resolved_at = None
    row.metadata_json = sanitize_for_log(metadata or {})
    session.flush()
    return row


def acknowledge_alert(
    session: Session, alert: OperationalAlert, *, actor_id: str
) -> OperationalAlert:
    if alert.status == "resolved":
        raise ValueError("resolved alert cannot be acknowledged")
    alert.status = "acknowledged"
    alert.acknowledged_by = actor_id
    alert.acknowledged_at = utcnow()
    session.flush()
    return alert


def resolve_alert(
    session: Session, alert: OperationalAlert, *, actor_id: str
) -> OperationalAlert:
    alert.status = "resolved"
    alert.resolved_by = actor_id
    alert.resolved_at = utcnow()
    session.flush()
    return alert


def alert_payload(row: OperationalAlert) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "kind": row.kind,
        "severity": row.severity,
        "title": row.title,
        "message": row.message,
        "status": row.status,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "fingerprint": row.fingerprint,
        "occurrence_count": row.occurrence_count,
        "first_occurred_at": row.first_occurred_at.isoformat(),
        "last_occurred_at": row.last_occurred_at.isoformat(),
        "acknowledged_by": row.acknowledged_by,
        "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        "resolved_by": row.resolved_by,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "metadata": row.metadata_json or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def emit_reflection_auto_rollback(
    session: Session,
    *,
    tenant_id: str,
    release_state_id: str,
    reason: str,
) -> OperationalAlert:
    """自动回滚的单一留痕入口：一条可处理告警 + 一条不可变审计。"""
    alert = raise_operational_alert(
        session,
        tenant_id=tenant_id,
        kind="reflection_auto_rollback",
        severity="critical",
        title="反思灰度已自动回滚",
        message=f"{reason}，覆盖比例已自动回滚到 0%。",
        fingerprint="reflection:auto-rollback",
        source_type="reflection_release",
        source_id=release_state_id,
        metadata={"reason": reason, "current_percent": 0},
    )
    from app.services.alert_delivery import enqueue_alert_webhook

    delivery = enqueue_alert_webhook(session, alert)
    record_audit(
        session,
        action="reflection_release_changed",
        tenant_id=tenant_id,
        actor_name="system",
        resource_type="reflection_release",
        resource_id=release_state_id,
        outcome="rollback",
        detail={
            "action": "automatic_rollback",
            "reason": reason,
            "current_percent": 0,
            "operational_alert_id": alert.id,
            "alert_delivery_id": delivery.id if delivery else None,
        },
    )
    return alert


__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_SEVERITIES",
    "ALLOWED_STATUSES",
    "acknowledge_alert",
    "alert_payload",
    "emit_reflection_auto_rollback",
    "raise_operational_alert",
    "resolve_alert",
]

"""管理员运行告警中心：查看、确认和解决需要人工关注的系统事件。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.base import utcnow
from app.models.workflow import OperationalAlert
from app.services.audit import record_audit
from app.services.alert_delivery import DEAD as DELIVERY_DEAD
from app.services.alert_delivery import SqlAlertDeliveryQueue
from app.services.operational_alerts import (
    ACTIVE_STATUSES,
    ALLOWED_SEVERITIES,
    ALLOWED_STATUSES,
    acknowledge_alert,
    alert_payload,
    resolve_alert,
)
from app.services.proactive_anomaly import (
    ProactiveTriggerDisabled,
    create_diagnosis_for_alert,
    scan_tenant_anomalies,
    trigger_status,
)
from app.services.scheduler_lease import SqlSchedulerLease
from app.services.task_dispatch import dispatch_task
from app.services.task_service import TaskService

router = APIRouter(prefix="/operational-alerts", tags=["运行告警"])


def _scoped_alert(session: Session, tenant_id: str, alert_id: str) -> OperationalAlert:
    row = (
        session.query(OperationalAlert)
        .filter(
            OperationalAlert.id == alert_id,
            OperationalAlert.tenant_id == tenant_id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "告警不存在"},
        )
    return row


def _summary(session: Session, tenant_id: str) -> dict[str, int]:
    grouped = dict(
        session.query(OperationalAlert.status, func.count(OperationalAlert.id))
        .filter(OperationalAlert.tenant_id == tenant_id)
        .group_by(OperationalAlert.status)
        .all()
    )
    critical = (
        session.query(func.count(OperationalAlert.id))
        .filter(
            OperationalAlert.tenant_id == tenant_id,
            OperationalAlert.severity == "critical",
            OperationalAlert.status.in_(ACTIVE_STATUSES),
        )
        .scalar()
        or 0
    )
    return {
        "open": int(grouped.get("open", 0)),
        "acknowledged": int(grouped.get("acknowledged", 0)),
        "critical": int(critical),
    }


@router.get("/proactive-trigger", summary="查看只读主动异常触发器状态")
def proactive_trigger_status(
    principal: Principal = Depends(get_current_principal),
) -> dict:
    principal.require("admin:manage")
    from app.config import get_settings

    payload = trigger_status(get_settings())
    scheduler = SqlSchedulerLease().status("proactive-anomaly")
    if scheduler is None:
        payload["scheduler"] = None
        return payload
    expires_at = scheduler.get("lease_expires_at")
    payload["scheduler"] = {
        "running": bool(
            scheduler.get("owner_id")
            and expires_at is not None
            and expires_at > utcnow()
        ),
        "heartbeat_at": scheduler.get("heartbeat_at"),
        "last_run_started_at": scheduler.get("last_run_started_at"),
        "last_run_finished_at": scheduler.get("last_run_finished_at"),
        "last_status": scheduler.get("last_status"),
        "has_error": bool(scheduler.get("last_error")),
        "last_result": scheduler.get("last_result") or {},
    }
    return payload


@router.post("/proactive-trigger/scan", summary="立即执行一次只读异常扫描")
def run_proactive_trigger(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("admin:manage")
    from app.config import get_settings

    try:
        result = scan_tenant_anomalies(
            session,
            tenant_id=principal.tenant_id,
            settings=get_settings(),
        )
    except ProactiveTriggerDisabled as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "proactive_trigger_disabled", "message": str(exc)},
        ) from exc
    record_audit(
        session,
        action="proactive_anomaly_scan_requested",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        actor_name=principal.user.username,
        resource_type="proactive_anomaly_trigger",
        outcome="success",
        detail={
            "read_only": True,
            "metrics_scanned": result["metrics_scanned"],
            "metrics_skipped": len(result.get("skipped") or []),
            "anomalies_found": result["anomalies_found"],
        },
    )
    session.commit()
    return result


@router.get("", summary="查看当前租户的运行告警")
def list_operational_alerts(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    severity: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    principal.require("admin:manage")
    if status_filter and status_filter not in ALLOWED_STATUSES:
        raise HTTPException(status_code=422, detail={"code": "invalid_status"})
    if severity and severity not in ALLOWED_SEVERITIES:
        raise HTTPException(status_code=422, detail={"code": "invalid_severity"})
    query = session.query(OperationalAlert).filter(
        OperationalAlert.tenant_id == principal.tenant_id
    )
    if status_filter:
        query = query.filter(OperationalAlert.status == status_filter)
    if severity:
        query = query.filter(OperationalAlert.severity == severity)
    rows = query.order_by(OperationalAlert.last_occurred_at.desc()).limit(limit).all()
    return {"items": [alert_payload(row) for row in rows], "summary": _summary(session, principal.tenant_id)}


@router.post(
    "/{alert_id}/create-diagnosis",
    summary="人工确认候选异常并创建诊断任务",
)
def create_proactive_diagnosis(
    alert_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> JSONResponse:
    principal.require("admin:manage")
    row = _scoped_alert(session, principal.tenant_id, alert_id)
    try:
        task, created = create_diagnosis_for_alert(
            session,
            alert=row,
            user=principal.user,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "not_proactive_candidate", "message": str(exc)},
        ) from exc
    session.commit()
    if created:
        dispatch_task(task.id)
        session.expire_all()
        task = TaskService(session).get(task.id, tenant_id=principal.tenant_id)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        content=jsonable_encoder(
            {
                "created": created,
                "task": TaskService(session).to_out(task),
                "alert": alert_payload(row),
            }
        ),
    )


@router.post("/{alert_id}/acknowledge", summary="确认已看到告警")
def acknowledge_operational_alert(
    alert_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("admin:manage")
    row = _scoped_alert(session, principal.tenant_id, alert_id)
    try:
        acknowledge_alert(session, row, actor_id=principal.user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "invalid_state", "message": str(exc)}) from exc
    record_audit(
        session,
        action="operational_alert_acknowledged",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        actor_name=principal.user.username,
        resource_type="operational_alert",
        resource_id=row.id,
    )
    session.commit()
    return alert_payload(row)


@router.post("/{alert_id}/resolve", summary="标记告警已解决")
def resolve_operational_alert(
    alert_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("admin:manage")
    row = _scoped_alert(session, principal.tenant_id, alert_id)
    resolve_alert(session, row, actor_id=principal.user.id)
    record_audit(
        session,
        action="operational_alert_resolved",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        actor_name=principal.user.username,
        resource_type="operational_alert",
        resource_id=row.id,
    )
    session.commit()
    return alert_payload(row)


@router.get("/deliveries", summary="查看告警外部投递队列")
def list_alert_deliveries(
    principal: Principal = Depends(get_current_principal),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    principal.require("admin:manage")
    allowed = {"pending", "claimed", "sent", "dead"}
    if status_filter and status_filter not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_delivery_status", "message": "投递状态无效"},
        )
    queue = SqlAlertDeliveryQueue()
    return {
        "items": queue.list_items(
            tenant_id=principal.tenant_id,
            status_filter=status_filter,
            limit=limit,
        ),
        "summary": queue.stats(tenant_id=principal.tenant_id),
    }


@router.post("/deliveries/{delivery_id}/replay", summary="重新投递一条 Webhook 死信")
def replay_alert_delivery(
    delivery_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("admin:manage")
    queue = SqlAlertDeliveryQueue()
    try:
        payload = queue.replay(delivery_id, tenant_id=principal.tenant_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "delivery_not_dead", "message": "只有 dead 投递可以重放"},
        ) from exc
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "投递记录不存在"},
        )
    record_audit(
        session,
        action="operational_alert_delivery_replayed",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        actor_name=principal.user.username,
        resource_type="operational_alert_delivery",
        resource_id=delivery_id,
        detail={"channel": "webhook", "previous_status": DELIVERY_DEAD},
    )
    session.commit()
    return payload


__all__ = ["router"]

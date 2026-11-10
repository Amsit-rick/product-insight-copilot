"""反思灰度与发布门禁接口。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_current_principal
from app.config import get_settings
from app.db.session import get_db
from app.services.audit import record_audit
from app.services.operational_alerts import emit_reflection_auto_rollback
from app.services.reflection_release import (
    get_or_create_release_state,
    refresh_release_decision,
    release_state_payload,
)

router = APIRouter(prefix="/experiments/reflection-release", tags=["反思灰度发布"])


class ReflectionReleaseAction(BaseModel):
    action: Literal["evaluate", "approve_promotion", "pause", "rollback", "resume"]
    percent: int | None = Field(default=None, ge=0, le=100)
    reason: str = Field(default="", max_length=500)


def _report(session: Session, tenant_id: str, *, days: int, force: bool = False) -> dict:
    settings = get_settings()
    row, decision, auto_rolled_back = refresh_release_decision(
        session, tenant_id, settings, days=days, force=force
    )
    if auto_rolled_back:
        emit_reflection_auto_rollback(
            session,
            tenant_id=tenant_id,
            release_state_id=row.id,
            reason=decision.reason,
        )
    return {
        "state": release_state_payload(row),
        "decision": decision.as_dict(),
        "scope": {
            "tenants": settings.reflection_rollout_tenants,
            "risk_levels": settings.reflection_rollout_risk_levels,
            "issue_types": settings.reflection_rollout_issue_types,
        },
        "config": {
            "global_enabled": settings.reflection_enabled,
            "ticket_enabled": settings.reflection_ticket_enabled,
            "report_enabled": settings.reflection_report_enabled,
            "configured_percent": settings.reflection_rollout_percent,
            "min_sample": settings.reflection_release_min_sample,
            "auto_guard": settings.reflection_rollout_auto_guard,
        },
        "window_days": days,
    }


@router.get("", summary="查看反思灰度状态与发布建议")
def reflection_release_report(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    days: int = Query(default=14, ge=1, le=90),
) -> dict:
    principal.require("task:read")
    response = _report(session, principal.tenant_id, days=days)
    session.commit()
    return response


@router.post("/decision", summary="评估或人工调整反思灰度发布状态")
def reflection_release_decision(
    payload: ReflectionReleaseAction,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    days: int = Query(default=14, ge=1, le=90),
) -> dict:
    principal.require("admin:manage")
    settings = get_settings()
    row = get_or_create_release_state(session, principal.tenant_id, settings)
    before = release_state_payload(row)

    if payload.action == "evaluate":
        response = _report(session, principal.tenant_id, days=days, force=True)
        session.commit()
        return response
    if payload.action == "pause":
        row.status = "paused"
    elif payload.action == "rollback":
        row.status = "rolled_back"
        row.current_percent = 0
        row.recommended_percent = 0
        row.recommendation = "rollback"
    elif payload.action == "resume":
        row.status = "active"
        if row.current_percent == 0:
            row.current_percent = payload.percent or min(10, settings.reflection_rollout_percent)
    elif payload.action == "approve_promotion":
        if payload.percent not in {10, 25, 50, 100}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "invalid_rollout_step", "message": "灰度比例只能取 10/25/50/100"},
            )
        if payload.percent <= row.current_percent:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "not_a_promotion", "message": "批准比例必须高于当前比例"},
            )
        row.status = "active"
        row.current_percent = payload.percent
        row.recommended_percent = payload.percent
        row.recommendation = "hold"

    row.version += 1
    row.updated_by = principal.user.id
    if payload.reason:
        row.reason = payload.reason
    session.flush()
    record_audit(
        session,
        action="reflection_release_changed",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        actor_name=principal.user.username,
        resource_type="reflection_release",
        resource_id=row.id,
        outcome=payload.action,
        detail={
            "action": payload.action,
            "reason": payload.reason,
            "before": before,
            "after": release_state_payload(row),
        },
    )
    session.commit()
    response = _report(session, principal.tenant_id, days=days)
    session.commit()
    return response


__all__ = ["router"]

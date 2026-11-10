"""售后政策接口（需求书 §6 / §7 知识库页）。

    GET  /policies                     政策清单（含版本、生效期、适用范围、可用性判定）
    POST /policies/documents           新建政策（内容入库 + 生效期与范围）
    POST /policies/documents/{id}/status  归档 / 启用
    POST /policies/search-test         政策检索测试（返回命中 + 被排除的版本与原因）

为什么把"可用性判定"直接做进接口返回：政策有版本与生效期，使用者最常问的是
"为什么这条政策没被用上"。如果只返回命中列表，这个问题只能靠人去猜或去翻库；
把 `usable` + `reason` 一起返回，界面就能直接告诉人"v1.0 已失效（2026-06-30）"。
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.enums import PolicyStatus
from app.services.policy_service import PolicyService

router = APIRouter(prefix="/policies", tags=["售后政策"])


class PolicyCreateRequest(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    version: str = Field(min_length=1, max_length=32, description="版本号，如 v3.0")
    content: str = Field(min_length=10, description="政策正文（Markdown）")
    effective_from: date | None = None
    effective_to: date | None = None
    scope: dict = Field(default_factory=dict, description="适用范围：platform/shop_id/member_level")
    note: str | None = None


class PolicyStatusRequest(BaseModel):
    status: str = Field(pattern="^(active|archived)$")
    # 变更理由：政策归档/启用是合规动作，审计里必须能看出"谁因为什么把它下线了"。
    # 此前接口只收 status，界面上的"理由必填"就只是前端仪式（写了也存不下来）——被界面同学打回来了。
    reason: str | None = Field(default=None, max_length=200)


class PolicySearchTestRequest(BaseModel):
    query: str = Field(min_length=2)
    top_k: int = Field(default=5, ge=1, le=20)
    at: datetime | None = None
    shop_id: str | None = None
    platform: str | None = None
    # 会员等级也是适用范围的一个维度（政策常写"仅 gold 会员"）。
    # `applicability()` 一直支持它，但接口没暴露，于是这类政策在接口下永远判"不适用（维度未知）"。
    member_level: str | None = None


@router.get("", summary="政策清单（含可用性判定）")
def list_policies(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    shop_id: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    member_level: str | None = Query(default=None, description="会员等级（适用范围维度之一）"),
    at: datetime | None = Query(default=None, description="按指定时间点判定可用性（默认现在）"),
):
    principal.require("policy:read")
    items = PolicyService(session).list_policies(
        tenant_id=principal.tenant_id,
        at=at,
        shop_id=shop_id,
        platform=platform,
        member_level=member_level,
    )
    # 回显的判定时点必须与**真正用于判定**的时点一致：`list_policies` 内部用的是 utcnow()，
    # 而这里原来返回 `datetime.now()`（本地时间）—— 界面同一屏会出现两个"现在"（17:24 / 09:24），
    # 看起来像 bug，排查时也会把人带偏。
    from app.models.base import utcnow

    return {
        "items": items,
        "count": len(items),
        "usable_count": sum(1 for item in items if item["usable"]),
        "at": (at or utcnow()).isoformat(),
    }


@router.post("/documents", status_code=status.HTTP_201_CREATED, summary="新建政策（版本 + 生效期）")
def create_policy(
    payload: PolicyCreateRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
):
    principal.require("policy:write")
    if payload.effective_to and payload.effective_from and payload.effective_to < payload.effective_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_effective_window", "message": "失效日期不能早于生效日期"},
        )
    row = PolicyService(session).create(
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        user=principal.user,
        title=payload.title,
        version=payload.version,
        content=payload.content,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        scope=payload.scope,
        note=payload.note,
    )
    return {
        "id": row.id,
        "title": row.title,
        "version": row.version,
        "status": row.status,
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "scope": row.scope,
        "document_id": row.document_id,
    }


@router.post("/documents/{policy_id}/status", summary="启用 / 归档政策")
def set_policy_status(
    policy_id: str,
    payload: PolicyStatusRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
):
    principal.require("policy:write")
    from app.models.ticket import PolicyDocument

    document = session.get(PolicyDocument, policy_id)
    if document is None or document.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "policy_not_found", "message": f"政策不存在: {policy_id}"},
        )
    row = PolicyService(session).set_status(
        document, payload.status, actor_id=principal.user.id, reason=payload.reason
    )
    return {"id": row.id, "version": row.version, "status": row.status, "reason": payload.reason}


@router.post("/search-test", summary="政策检索测试（含被排除版本与原因）")
def search_test(
    payload: PolicySearchTestRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
):
    principal.require("policy:read")
    return PolicyService(session).search_test(
        tenant_id=principal.tenant_id,
        query=payload.query,
        at=payload.at,
        shop_id=payload.shop_id,
        platform=payload.platform,
        member_level=payload.member_level,
        top_k=payload.top_k,
    )


@router.get("/status-options", summary="政策状态取值")
def status_options(principal: Principal = Depends(get_current_principal)):
    principal.require("policy:read")
    return {"statuses": [item.value for item in PolicyStatus]}

"""指标级密级策略的管理接口（设计文档 11 节 / README §7.3）。

为什么需要它：策略此前只能直接改数据库，导致"权限到底配了什么"无法在界面上核对。
这里提供最小可用的管理面（仅管理员）：

- `GET /access-policies`：列出本租户全部策略（含条件），用于人工核对"谁对什么资源有什么权限"；
- `POST /access-policies`：新增一条策略（含有效期 / 时间窗 / 行级数据范围等条件）；
- `DELETE /access-policies/{id}`：删除（回收授权，等价于"到期即失效"之外的手动收权）。

安全约定：
- 只有 `admin:manage` 能调用（普通角色连"有哪些策略"都不该看到——策略清单本身是信息）；
- 资源类型 / 效果 / 动作都做白名单校验，`conditions` 里的行级维度也走白名单
  （避免把策略表变成注入列名的入口）；
- 写操作记审计。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.identity import AccessPolicy, User
from app.schemas.policy import (
    AccessPolicyCreate,
    AccessPolicyOut,
    AuthorizationSimulateRequest,
    PolicySimulateRequest,
)
from app.services.audit import record_audit
from app.services.authorization import (
    AuthorizationRequest,
    AuthorizationResource,
    AuthorizationService,
    AuthorizationSubject,
)
from app.auth.rbac import ROLE_ACTIONS
from app.services.metric_access import (
    METRIC_RESOURCE_TYPES,
    ROW_FILTER_DIMENSIONS,
    MetricScope,
    check_dimension_values,
    explain_access,
)
from app.services.policy_conditions import (
    ConditionValidationError,
    validate_policy_conditions,
)

router = APIRouter(prefix="/access-policies", tags=["访问策略"])

ALLOWED_EFFECTS = {"allow", "deny", "require_approval"}
ALLOWED_SUBJECTS = {"role", "user"}
ALLOWED_RESOURCE_TYPES = set(METRIC_RESOURCE_TYPES) | {
    "task",
    "task_replay",
    "ticket",
    "knowledge_base",
    "document",
    "tool",
}
ALLOWED_ACTIONS = set().union(*ROLE_ACTIONS.values()) | {
    "metric:read",
    "metric:read_all",
    "read",
    "*",
}
# 条件里允许出现的键：多一个键就意味着多一种判定语义，必须显式登记
ALLOWED_CONDITION_KEYS = {
    "min_clearance",
    "valid_from",
    "valid_until",
    "time_window",
    "row_filters",
    "timezone",
    "departments",
    "risk_tiers",
    "classifications",
    "min_amount",
    "max_amount",
    "tool_names",
}


def _validate(payload: AccessPolicyCreate) -> None:
    if payload.resource_type not in ALLOWED_RESOURCE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_resource_type", "message": f"resource_type 只能是 {sorted(ALLOWED_RESOURCE_TYPES)}"},
        )
    if payload.effect not in ALLOWED_EFFECTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_effect", "message": "effect 只能是 allow / deny / require_approval"},
        )
    if payload.subject_type not in ALLOWED_SUBJECTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_subject_type", "message": "subject_type 只能是 role / user"},
        )
    if not payload.actions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_actions", "message": "actions 不能为空"},
        )
    unknown_actions = sorted(set(payload.actions) - ALLOWED_ACTIONS)
    if unknown_actions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_actions", "message": f"不支持的动作: {unknown_actions}"},
        )
    unknown_keys = sorted(set(payload.conditions or {}) - ALLOWED_CONDITION_KEYS)
    if unknown_keys:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_conditions", "message": f"不支持的条件键: {unknown_keys}"},
        )
    row_filters = (payload.conditions or {}).get("row_filters") or {}
    if row_filters and payload.resource_type not in METRIC_RESOURCE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_row_filters", "message": "row_filters 仅适用于指标类资源"},
        )
    if row_filters:
        bad_dims = sorted(set(row_filters) - set(ROW_FILTER_DIMENSIONS))
        if bad_dims:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "invalid_row_filters",
                    "message": f"行级维度只能是 {list(ROW_FILTER_DIMENSIONS)}，收到 {bad_dims}",
                },
            )
    try:
        validate_policy_conditions(
            payload.conditions,
            allow_row_filters=payload.resource_type in METRIC_RESOURCE_TYPES,
            row_filter_dimensions=ROW_FILTER_DIMENSIONS,
        )
    except ConditionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_conditions", "message": str(exc)},
        ) from exc


def _to_out(row: AccessPolicy) -> AccessPolicyOut:
    return AccessPolicyOut(
        id=row.id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        effect=row.effect,
        actions=list(row.actions or []),
        conditions=dict(row.conditions or {}),
        created_at=row.created_at,
    )


@router.get("", response_model=list[AccessPolicyOut], summary="指标密级策略列表")
def list_policies(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> list[AccessPolicyOut]:
    principal.require("admin:manage")
    rows = (
        session.query(AccessPolicy)
        .filter(AccessPolicy.tenant_id.in_([principal.tenant_id, "*"]))
        .order_by(AccessPolicy.resource_type, AccessPolicy.resource_id, AccessPolicy.subject_id)
        .all()
    )
    return [_to_out(row) for row in rows]


@router.post("", response_model=AccessPolicyOut, status_code=status.HTTP_201_CREATED, summary="新增策略")
def create_policy(
    payload: AccessPolicyCreate,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> AccessPolicyOut:
    principal.require("admin:manage")
    _validate(payload)
    duplicate = (
        session.query(AccessPolicy)
        .filter(AccessPolicy.tenant_id == principal.tenant_id)
        .filter(AccessPolicy.subject_type == payload.subject_type)
        .filter(AccessPolicy.subject_id == payload.subject_id)
        .filter(AccessPolicy.resource_type == payload.resource_type)
        .filter(AccessPolicy.resource_id == payload.resource_id)
        .first()
    )
    if duplicate is not None:
        # 同一主体+资源的重复策略会让"为什么生效"变得难查，因此直接拒绝并提示改哪条
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_policy",
                "message": f"已存在同主体同资源的策略（id={duplicate.id}），请先删除或修改它",
            },
        )
    row = AccessPolicy(
        tenant_id=principal.tenant_id,
        subject_type=payload.subject_type,
        subject_id=payload.subject_id,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        effect=payload.effect,
        actions=list(payload.actions),
        conditions=dict(payload.conditions or {}),
    )
    session.add(row)
    session.flush()
    record_audit(
        session,
        action="access_policy_created",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        resource_type="access_policy",
        resource_id=row.id,
        detail={
            "subject": f"{payload.subject_type}:{payload.subject_id}",
            "resource": f"{payload.resource_type}:{payload.resource_id}",
            "effect": payload.effect,
        },
    )
    session.commit()
    return _to_out(row)


@router.post("/simulate", summary="权限预演：某账号/角色能不能读某指标，为什么")
def simulate_policy(
    payload: PolicySimulateRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    """把"判定过程"整条返回：结论 + 逐条策略的结论 + 生效的行级范围。

    为什么要有它：管理员此前只能看到策略清单，**看不出"某个账号为什么看不到某个指标"**，
    只能靠人脑模拟判定规则（而规则里有 deny 优先、白名单、有效期、时间窗、行级范围）。
    这里直接跑**与真实请求完全相同的那份判定实现**（`explain_access`），不做任何近似。

    只读操作：不写策略、不改权限，但要记审计（"谁在什么时候查过谁的权限"本身是敏感信息）。
    """
    principal.require("admin:manage")

    # 主体解析：优先按账号取真实的角色与密级（这样预演结果就和该账号真实看到的完全一致）
    username = (payload.username or "").strip()
    user_id: str | None = None
    if username:
        user = (
            session.query(User)
            .filter(User.tenant_id == principal.tenant_id)
            .filter(User.username == username)
            .first()
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "user_not_found", "message": f"账号不存在：{username}"},
            )
        roles = list(user.role_codes)
        clearance = user.security_level
        user_id = user.id
    else:
        roles = [str(item) for item in payload.roles]
        clearance = payload.clearance

    explanation = explain_access(
        session,
        tenant_id=principal.tenant_id,
        roles=roles,
        user_id=user_id,
        clearance=clearance,
        resource_name=payload.metric,
        resource_type=payload.resource_type,
    )

    # 顺带验证"某个具体维度取值是否在范围内"：权限问题不能被伪装成"这个渠道没数据"
    violation: str | None = None
    requested = {key: value for key, value in (payload.requested or {}).items() if value}
    if requested:
        unknown = sorted(set(requested) - set(ROW_FILTER_DIMENSIONS))
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "invalid_dimension",
                    "message": f"待验证维度只能是 {list(ROW_FILTER_DIMENSIONS)}，收到 {unknown}",
                },
            )
        scope = MetricScope(explanation.decision.allowed, explanation.decision.reason,
                            explanation.effective_row_filters, explanation.decision.matched_policy_id)
        violation = check_dimension_values(scope, requested)

    result = {
        "subject": {
            "username": username or None,
            "user_id": user_id,
            "roles": roles,
            "clearance": clearance,
            "source": "account" if username else "explicit",
        },
        "resource": {"type": payload.resource_type, "name": payload.metric},
        "requested": requested,
        "request_violation": violation,
        **explanation.as_dict(),
    }

    record_audit(
        session,
        action="policy_simulated",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        resource_type="access_policy",
        resource_id=payload.metric,
        detail={
            "subject": username or f"roles={roles},clearance={clearance}",
            "resource": f"{payload.resource_type}:{payload.metric}",
            "allowed": explanation.decision.allowed,
            # 只记结论，不记原因全文，避免审计里堆长文本
        },
    )
    session.commit()
    return result


@router.post(
    "/authorize-simulate",
    summary="统一授权预演：RBAC、租户与资源 ABAC 综合判定",
)
def simulate_authorization(
    payload: AuthorizationSimulateRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("admin:manage")
    if payload.resource_type not in ALLOWED_RESOURCE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_resource_type",
                "message": f"resource_type 只能是 {sorted(ALLOWED_RESOURCE_TYPES)}",
            },
        )
    if payload.action not in ALLOWED_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_action", "message": f"不支持的动作: {payload.action}"},
        )

    username = (payload.username or "").strip()
    user_id: str | None = None
    if username:
        user = (
            session.query(User)
            .filter(User.tenant_id == principal.tenant_id)
            .filter(User.username == username)
            .first()
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "user_not_found", "message": f"账号不存在：{username}"},
            )
        roles = list(user.role_codes)
        clearance = user.security_level or 0
        department = user.department
        user_id = user.id
    else:
        roles = [str(item) for item in payload.roles]
        clearance = payload.clearance or 0
        department = payload.department

    context = dict(payload.context or {})
    resource = AuthorizationResource(
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        tenant_id=payload.resource_tenant_id or principal.tenant_id,
        department=context.get("department"),
        risk_tier=context.get("risk_tier"),
        amount=context.get("amount"),
        classification=context.get("classification"),
        tool_name=context.get("tool_name"),
        attributes=context,
    )
    decision = AuthorizationService(session).decide(
        AuthorizationRequest(
            subject=AuthorizationSubject(
                tenant_id=principal.tenant_id,
                user_id=user_id,
                roles=roles,
                department=department,
                clearance=clearance,
            ),
            action=payload.action,
            resource=resource,
            context=context,
        )
    )
    result = {
        "subject": {
            "username": username or None,
            "user_id": user_id,
            "roles": roles,
            "clearance": clearance,
            "department": department,
            "source": "account" if username else "explicit",
        },
        "resource": {
            "type": resource.resource_type,
            "id": resource.resource_id,
            "tenant_id": resource.tenant_id,
        },
        "action": payload.action,
        **decision.as_dict(),
    }
    record_audit(
        session,
        action="authorization_simulated",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        detail={
            "subject": username or f"roles={roles}",
            "action": payload.action,
            "decision": decision.decision,
            "matched_policy_ids": decision.matched_policy_ids,
        },
    )
    session.commit()
    return result


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除策略")
def delete_policy(
    policy_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> None:
    principal.require("admin:manage")
    row = (
        session.query(AccessPolicy)
        .filter(AccessPolicy.id == policy_id)
        .filter(AccessPolicy.tenant_id.in_([principal.tenant_id, "*"]))
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "策略不存在"},
        )
    record_audit(
        session,
        action="access_policy_deleted",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        resource_type="access_policy",
        resource_id=policy_id,
        detail={"subject": f"{row.subject_type}:{row.subject_id}", "resource": f"{row.resource_type}:{row.resource_id}"},
    )
    session.delete(row)
    session.commit()

"""诊断任务接口（设计文档 13 节）。

    POST /tasks                        创建并提交诊断任务（幂等键去重）
    GET  /tasks                        任务列表（工作台）
    GET  /tasks/summary                各状态任务数（工作台看板）
    GET  /tasks/{id}                   任务详情
    GET  /tasks/{id}/events            执行事件流（任务进度页）
    GET  /tasks/{id}/trace             Agent 运行与工具调用轨迹（审计视角）
    GET  /tasks/{id}/report            该任务最新报告
    POST /tasks/{id}/cancel            取消（在节点边界生效）
    POST /tasks/{id}/retry             失败/中断后重试
    POST /tasks/{id}/approve           人工审核结论（approve/reject/modify），恢复工作流
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import Pagination, Principal, get_current_principal, get_pagination
from app.config import get_settings
from app.db.session import get_db
from app.models.enums import TaskStatus
from app.models.workflow import ToolCall
from app.schemas.common import ListResponse, PageMeta
from app.schemas.task import (
    TaskApproveRequest,
    TaskCancelRequest,
    TaskCreateRequest,
    TaskEventOut,
    TaskOut,
    TaskRetryRequest,
)
from app.services.cost import budget_state, task_cost, tenant_daily_budget_exceeded
from app.services.authorization import (
    AuthorizationRequest,
    AuthorizationResource,
    AuthorizationService,
    subject_from_principal,
)
from app.services.audit import record_audit
from app.services.limits import get_rate_limiter
from app.services.task_dispatch import dispatch_task, resolve_backend
from app.services.task_execution import TaskExecutionService
from app.services.task_live import TaskLiveService, task_event_stream
from app.services.task_service import (
    TaskConflict,
    TaskNotFound,
    TaskRevisionConflict,
    TaskService,
)
from app.services.task_replay import TaskReplayService
from app.services.workflow_runner import get_runner

router = APIRouter(prefix="/tasks", tags=["诊断任务"])


def _snapshot_out(snapshot) -> dict:  # noqa: ANN001
    return {
        "snapshot_id": snapshot.id,
        "task_id": snapshot.task_id,
        "schema_version": snapshot.schema_version,
        "mode": "playback",
        "reran_model_or_tools": False,
        "digest": snapshot.digest,
        "created_by": snapshot.created_by,
        "created_at": snapshot.created_at,
        "package": snapshot.package,
    }


def _authorize_replay(
    *, task_id: str, principal: Principal, session: Session
) -> None:
    service = TaskService(session)
    try:
        task = service.get(task_id, tenant_id=principal.tenant_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    decision = AuthorizationService(session).decide(
        AuthorizationRequest(
            subject=subject_from_principal(principal),
            action="task:replay",
            resource=AuthorizationResource(
                resource_type="task_replay",
                resource_id=task.id,
                tenant_id=task.tenant_id,
                risk_tier=task.risk_level,
            ),
            context={"risk_tier": task.risk_level},
        )
    )
    from app.db.session import write_scope

    with write_scope() as audit_session:
        record_audit(
            audit_session,
            action="authorization_decision",
            tenant_id=principal.tenant_id,
            actor_id=principal.user.id,
            actor_name=principal.user.username,
            resource_type="task_replay",
            resource_id=task.id,
            outcome=decision.decision,
            detail={
                "action": "task:replay",
                "decision": decision.decision,
                "reason": decision.reason,
                "matched_policy_ids": decision.matched_policy_ids,
            },
        )
    if decision.allowed:
        return
    code = "approval_required" if decision.requires_approval else "resource_forbidden"
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": code, **decision.as_dict()},
    )


@router.post(
    "",
    response_model=TaskOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建诊断任务",
)
def create_task(
    payload: TaskCreateRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> TaskOut:
    principal.require("task:create")

    # 限流：按「租户 + 用户」计数，防止脚本误刷把成本烧掉
    limiter = get_rate_limiter()
    allowed, retry_after = limiter.check(
        f"task_create:{principal.tenant_id}:{principal.user.id}",
        limit=get_settings().task_submit_rate_limit_per_minute,
        window_seconds=60,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": f"提交过于频繁，请 {retry_after} 秒后重试"
                f"（上限 {get_settings().task_submit_rate_limit_per_minute} 次/分钟）",
            },
            headers={"Retry-After": str(retry_after)},
        )

    # 日预算：超限直接拒绝创建，避免"先把钱花完再说"
    daily = tenant_daily_budget_exceeded(session, principal.tenant_id)
    if daily.get("exceeded"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "budget_exceeded",
                "message": f"今日成本已达上限 {daily['limit']} {daily.get('currency', '')}"
                f"（当前 {daily['amount']}），请调整预算或明日再试",
            },
        )

    service = TaskService(session)
    task, created = service.create(
        user=principal.user,
        query=payload.query,
        filters=payload.to_filters(),
        title=payload.title,
        idempotency_key=payload.idempotency_key,
        force_human_review=payload.force_human_review,
    )
    if created:
        # 请求的知识库必须在权限范围内：这里先做一次早失败，避免任务跑起来才被拒
        requested = set(payload.knowledge_bases or [])
        outside = sorted(requested - principal.knowledge_bases)
        if outside:
            task.error = {
                "code": "permission_denied",
                "message": f"请求的知识库超出权限范围: {outside}",
            }
            task.status = "failed"
            session.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "permission_denied", "message": f"无权访问: {outside}"},
            )
    session.commit()

    if created:
        # 统一分发：inline 同步跑 / threadpool 本进程 / queue 交给 worker（见 services/task_dispatch.py）
        dispatch_task(task.id)
        session.expire_all()
        task = service.get(task.id)

    return TaskOut(**service.to_out(task))


@router.get("", response_model=ListResponse[TaskOut], summary="任务列表")
def list_tasks(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    pagination: Pagination = Depends(get_pagination),
    status_filter: str | None = Query(default=None, alias="status"),
    mine: bool = Query(default=False, description="只看我创建的"),
    keyword: str | None = Query(default=None, max_length=64),
) -> ListResponse[TaskOut]:
    principal.require("task:read")
    service = TaskService(session)
    items, total = service.list(
        tenant_id=principal.tenant_id,
        status=status_filter,
        user_id=principal.user.id if mine else None,
        page=pagination.page,
        page_size=pagination.page_size,
        keyword=keyword,
    )
    return ListResponse[TaskOut](
        items=[TaskOut(**service.to_out(item)) for item in items],
        meta=PageMeta(total=total, page=pagination.page, page_size=pagination.page_size),
    )


@router.get("/summary", summary="工作台看板：各状态任务数")
def task_summary(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("task:read")
    service = TaskService(session)
    counts = service.status_counts(principal.tenant_id)
    return {
        "counts": counts,
        "pending_review": counts.get("waiting_human", 0),
        "failed": counts.get("failed", 0),
        "running": counts.get("running", 0),
    }


@router.get("/{task_id}", response_model=TaskOut, summary="任务详情")
def get_task(
    task_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> TaskOut:
    principal.require("task:read")
    service = TaskService(session)
    try:
        task = service.get(task_id, tenant_id=principal.tenant_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return TaskOut(**service.to_out(task))


@router.get("/{task_id}/tree", summary="任务父子执行树")
def task_tree(
    task_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("task:read")
    service = TaskService(session)
    try:
        service.get(task_id, tenant_id=principal.tenant_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return TaskExecutionService(session).tree(task_id)


@router.get("/{task_id}/snapshot", summary="任务实时状态快照")
def task_snapshot(
    task_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("task:read")
    try:
        return TaskLiveService(session).snapshot(
            task_id=task_id,
            tenant_id=principal.tenant_id,
        )
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{task_id}/stream", summary="任务快照与增量事件流")
def task_stream(
    task_id: str,
    max_seconds: int = Query(default=60, ge=1, le=300),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> StreamingResponse:
    principal.require("task:read")
    try:
        TaskService(session).get(task_id, tenant_id=principal.tenant_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StreamingResponse(
        task_event_stream(
            task_id=task_id,
            tenant_id=principal.tenant_id,
            max_seconds=max_seconds,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/{task_id}/events",
    response_model=ListResponse[TaskEventOut],
    summary="任务执行事件流",
)
def task_events(
    task_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    after: str | None = Query(default=None, description="从某个事件 id 之后拉取（增量轮询）"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> ListResponse[TaskEventOut]:
    principal.require("task:read")
    service = TaskService(session)
    service.get(task_id, tenant_id=principal.tenant_id)
    events = service.events(task_id, after=after, limit=limit)
    return ListResponse[TaskEventOut](
        items=[TaskEventOut.model_validate(item) for item in events],
        meta=PageMeta(total=len(events), page=1, page_size=limit),
    )


@router.get("/{task_id}/trace", summary="Agent 运行与工具调用轨迹")
def task_trace(
    task_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("task:read")
    service = TaskService(session)
    task = service.get(task_id, tenant_id=principal.tenant_id)
    runs = service.agent_runs(task_id)
    tool_calls = (
        session.query(ToolCall)
        .filter(ToolCall.task_id == task_id)
        .order_by(ToolCall.created_at.asc())
        .all()
    )
    return {
        "task_id": task_id,
        "status": task.status,
        "risk_level": task.risk_level,
        "retry_count": task.retry_count,
        "agent_runs": [
            {
                "id": run.id,
                "agent_code": run.agent_code,
                "node": run.node,
                "status": run.status,
                "attempt": run.attempt,
                "model": run.model,
                "prompt_version": run.prompt_version,
                "latency_ms": run.latency_ms,
                "total_tokens": run.total_tokens,
                "error": run.error,
                "summary": (run.output or {}).get("summary"),
                "findings": len((run.output or {}).get("findings") or []),
                # 该 Agent 配置的模型 vs 本次是否真的用上了模型输出
                "model_used": bool((run.output or {}).get("model_used")),
                # 路由来源：canary（灰度）/ override / global，灰度期间用来归因 A/B
                "model_source": (run.output or {}).get("model_source"),
                "canary_bucket": (run.output or {}).get("canary_bucket"),
            }
            for run in runs
        ],
        "tool_calls": [
            {
                "id": call.id,
                "tool_name": call.tool_name,
                "status": call.status,
                "policy_decision": call.policy_decision,
                "latency_ms": call.latency_ms,
                "arguments": call.arguments,
                "result_summary": call.result_summary,
                "result_digest": call.result_digest,
                "replay_policy": call.replay_policy,
                "side_effect_level": call.side_effect_level,
                "idempotency_key": call.idempotency_key,
                "replayed": bool(call.replayed),
                "approval_reference": call.approval_reference,
            }
            for call in tool_calls
        ],
        "totals": {
            "tokens": sum(run.total_tokens or 0 for run in runs),
            "latency_ms": sum(run.latency_ms or 0 for run in runs),
            "tool_calls": len(tool_calls),
            "denied_tool_calls": sum(
                1 for call in tool_calls if call.policy_decision == "deny"
            ),
        },
        # 成本与预算：本次任务实际花了多少、是否触及上限（前端直接展示）
        "cost": task_cost(session, task_id).as_dict(),
        "budget": budget_state(session, task_id),
    }


@router.post(
    "/{task_id}/replay-snapshots",
    status_code=status.HTTP_201_CREATED,
    summary="生成任务历史播放快照（不重新执行模型或工具）",
)
def create_task_replay_snapshot(
    task_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    _authorize_replay(task_id=task_id, principal=principal, session=session)
    try:
        snapshot, created = TaskReplayService(session).create_snapshot(
            task_id=task_id,
            tenant_id=principal.tenant_id,
            created_by=principal.user.id,
        )
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    response = _snapshot_out(snapshot)
    response["created"] = created
    return response


@router.get(
    "/{task_id}/replay",
    summary="播放最近一次任务快照（不重新执行模型或工具）",
)
def get_task_replay(
    task_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    _authorize_replay(task_id=task_id, principal=principal, session=session)
    try:
        snapshot = TaskReplayService(session).latest_snapshot(
            task_id=task_id,
            tenant_id=principal.tenant_id,
        )
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "replay_snapshot_not_found", "message": "该任务尚未生成回放快照"},
        )
    return _snapshot_out(snapshot)


@router.post(
    "/{task_id}/replay/verify",
    summary="校验最近一次任务回放快照完整性",
)
def verify_task_replay(
    task_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    _authorize_replay(task_id=task_id, principal=principal, session=session)
    try:
        service = TaskReplayService(session)
        snapshot = service.latest_snapshot(task_id=task_id, tenant_id=principal.tenant_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "replay_snapshot_not_found", "message": "该任务尚未生成回放快照"},
        )
    verification = service.verify_snapshot(snapshot)
    return {
        "snapshot_id": snapshot.id,
        "task_id": task_id,
        "valid": verification.valid,
        "expected_digest": verification.expected_digest,
        "actual_digest": verification.actual_digest,
    }


@router.get("/{task_id}/report", summary="任务最新报告")
def task_report(
    task_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
):
    from app.api.routes_reports import build_report_out

    principal.require("report:read")
    service = TaskService(session)
    service.get(task_id, tenant_id=principal.tenant_id)
    report = service.latest_report(task_id)
    if report is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "report_not_found", "message": "该任务尚未生成报告"},
        )
    return build_report_out(session, report)


@router.post("/{task_id}/cancel", response_model=TaskOut, summary="取消任务")
def cancel_task(
    task_id: str,
    payload: TaskCancelRequest | None = None,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> TaskOut:
    principal.require("task:cancel")
    payload = payload or TaskCancelRequest()
    service = TaskService(session)
    try:
        task = service.request_cancel(
            task_id,
            tenant_id=principal.tenant_id,
            actor=principal.user,
            expected_revision=payload.expected_revision,
        )
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TaskRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return TaskOut(**service.to_out(task))


@router.post("/{task_id}/retry", response_model=TaskOut, summary="重试任务")
def retry_task(
    task_id: str,
    payload: TaskRetryRequest | None = None,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> TaskOut:
    principal.require("task:create")
    payload = payload or TaskRetryRequest()
    service = TaskService(session)
    try:
        task = service.prepare_retry(
            task_id,
            tenant_id=principal.tenant_id,
            reset_retry_count=payload.reset_retry_count,
            actor=principal.user,
            reason=payload.reason,
            expected_revision=payload.expected_revision,
        )
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TaskRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    dispatch_task(task.id)
    session.expire_all()
    return TaskOut(**service.to_out(service.get(task.id)))


@router.post("/{task_id}/approve", response_model=TaskOut, summary="提交人工审核结论")
def approve_task(
    task_id: str,
    payload: TaskApproveRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> TaskOut:
    principal.require("task:approve")
    service = TaskService(session)
    try:
        task = service.get(task_id, tenant_id=principal.tenant_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if task.status != "waiting_human":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_waiting_human",
                "message": f"任务当前状态为 {task.status}，无需审核",
            },
        )

    try:
        task = service.claim_revision(
            task_id,
            tenant_id=principal.tenant_id,
            expected_revision=payload.expected_revision,
            allowed_statuses={TaskStatus.WAITING_HUMAN.value},
            invalid_status_message=f"任务当前状态为 {task.status}，无需审核",
        )
    except TaskRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    decision = {
        "action": payload.action,
        "reviewer_id": principal.user.id,
        "comment": payload.comment,
        "modifications": payload.modifications or {},
        "_expected_revision": task.state_revision,
    }
    backend = resolve_backend()
    if backend != "inline":
        # 异步恢复（threadpool / queue）：先把状态推进到 running 并释放人工等待，
        # 否则调用方拿到的响应会停留在 waiting_human，看起来像"没生效"
        task.status = "running"
        task.waiting_reason = None
    session.commit()
    dispatch_task(task_id, kind="resume", payload=decision)
    session.expire_all()
    return TaskOut(**service.to_out(service.get(task_id)))

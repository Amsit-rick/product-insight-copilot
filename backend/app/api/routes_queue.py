"""队列运维接口（仅管理员）：把队列里的东西**看清楚**，并把死信**放回队列**。

为什么需要它：`/health` 的 `execution.queue` 只给出**计数**（pending/claimed/done/dead）——
"有 3 条死信"和"这 3 条是哪些任务、为什么死的"是两回事。中间那块缺失导致运维只能干瞪眼：
任务明明失败了，却看不出失败在执行还是在队列、也看不到错误原因。

三个入口：
- `GET  /queue/items?status=dead`：列队列项（默认带任务的问题与状态，便于定位）；
- `POST /queue/items/{id}/replay`：把**这条死信**重新放回队列；
- `POST /queue/replay-dead`：把所有死信一次性放回（例如一次 worker bug 导致批量判死）。

与 `POST /tasks/{id}/retry` 的分工：后者面向**任务**（新建队列项、任务侧 `retry_count` 累加），
前者面向**队列项**（复用这条死信本身，保留 `last_error` 与重放历史）。
两者都只放开队列，**不会**绕过任何权限或人工审核。

安全：仅 `admin:manage`；重放会记审计（"谁把哪条死信放回去了"必须可追溯）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.workflow import AnalysisTask
from app.services.audit import record_audit
from app.services.task_queue import DEAD, get_task_queue

router = APIRouter(prefix="/queue", tags=["队列运维"])

# 允许筛选的队列状态（白名单，避免把任意字符串当过滤条件传下去）
ALLOWED_STATUS = {"pending", "claimed", "done", "dead"}


def _with_task_context(session: Session, items: list[dict], tenant_id: str) -> list[dict]:
    """给队列项补上"这是哪个任务、任务现在什么状态、问的是什么"，否则光看 task_id 没法判断。"""
    task_ids = [item["task_id"] for item in items]
    if not task_ids:
        return items
    tasks = {
        row.id: row
        for row in session.query(AnalysisTask)
        .filter(AnalysisTask.id.in_(task_ids))
        .filter(AnalysisTask.tenant_id == tenant_id)
        .all()
    }
    enriched: list[dict] = []
    for item in items:
        task = tasks.get(item["task_id"])
        enriched.append(
            {
                **item,
                "task_status": task.status if task else None,
                "task_query": (task.query[:120] if task and task.query else None),
                "task_missing": task is None,
            }
        )
    return enriched


@router.get("/items", summary="队列项列表（死信排查用）")
def list_queue_items(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    principal.require("admin:manage")
    if status_filter and status_filter not in ALLOWED_STATUS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_status", "message": f"status 只能是 {sorted(ALLOWED_STATUS)}"},
        )
    queue = get_task_queue()
    items = [item.as_dict() for item in queue.list_items(status_filter=status_filter, limit=limit)]
    return {
        "stats": queue.stats(),
        "items": _with_task_context(session, items, principal.tenant_id),
    }


@router.post("/items/{item_id}/replay", summary="把一条死信重新放回队列")
def replay_queue_item(
    item_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("admin:manage")
    queue = get_task_queue()
    outcome = queue.replay(item_id)
    if outcome == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "队列项不存在"},
        )
    if outcome == "not_dead":
        # 只允许重放死信：把 pending/claimed/done 的项重新放回会造成"同一任务两条队列项"，
        # 那正是多副本下最容易出乱子的形态。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "not_dead", "message": "只有 dead 状态的队列项可以重放"},
        )
    record_audit(
        session,
        action="queue_item_replayed",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        resource_type="queue_item",
        resource_id=item_id,
        detail={"outcome": outcome},
    )
    session.commit()
    return {"item_id": item_id, "replayed": True, "stats": queue.stats()}


@router.post("/replay-dead", summary="把所有死信重新放回队列")
def replay_all_dead(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("admin:manage")
    replayed = get_task_queue().replay_all()
    record_audit(
        session,
        action="queue_dead_replayed_all",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        resource_type="queue",
        resource_id="dead",
        detail={"replayed": replayed},
    )
    session.commit()
    return {"replayed": replayed, "stats": get_task_queue().stats()}

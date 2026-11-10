"""Durable task snapshot plus incremental event streaming."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import datetime

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models.enums import TaskStatus
from app.models.workflow import AgentRun, AnalysisEvent, ToolCall
from app.services.task_runtime import task_phase_for_node
from app.services.task_execution import TaskExecutionService
from app.services.task_service import TaskService

TERMINAL_STATUSES = {
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _event_out(event: AnalysisEvent, *, event_seq: int) -> dict:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "event_seq": event_seq,
        "node": event.node,
        "phase": task_phase_for_node(event.node).code,
        "level": event.level,
        "message": event.message,
        "payload": event.payload or {},
        "created_at": _iso(event.created_at),
    }


class TaskLiveService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def snapshot(self, *, task_id: str, tenant_id: str, event_limit: int = 200) -> dict:
        task_service = TaskService(self.session)
        task = task_service.get(task_id, tenant_id=tenant_id)
        task_out = task_service.to_out(task)
        if not task_out.get("current_phase"):
            task_out["current_phase"] = task_phase_for_node(task.current_node).code

        event_query = self.session.query(AnalysisEvent).filter(AnalysisEvent.task_id == task_id)
        total_events = int(event_query.with_entities(func.count(AnalysisEvent.id)).scalar() or 0)
        offset = max(total_events - event_limit, 0)
        events = (
            event_query.order_by(AnalysisEvent.created_at.asc(), AnalysisEvent.id.asc())
            .offset(offset)
            .limit(event_limit)
            .all()
        )
        event_items = [
            _event_out(event, event_seq=offset + index)
            for index, event in enumerate(events, start=1)
        ]

        runs = (
            self.session.query(AgentRun)
            .filter(AgentRun.task_id == task_id)
            .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
            .all()
        )
        calls = (
            self.session.query(ToolCall)
            .filter(ToolCall.task_id == task_id)
            .order_by(ToolCall.created_at.asc(), ToolCall.id.asc())
            .all()
        )
        return {
            "schema_version": "task-live.v1",
            "task": task_out,
            "state_revision": int(task.state_revision or 0),
            "event_cursor": event_items[-1]["id"] if event_items else None,
            "event_count": total_events,
            "events": event_items,
            "task_tree": TaskExecutionService(self.session).tree(task_id),
            "agent_runs": [
                {
                    "id": run.id,
                    "agent_code": run.agent_code,
                    "node": run.node,
                    "phase": task_phase_for_node(run.node).code,
                    "status": run.status,
                    "attempt": run.attempt,
                    "latency_ms": run.latency_ms,
                    "created_at": _iso(run.created_at),
                }
                for run in runs
            ],
            "tool_calls": [
                {
                    "id": call.id,
                    "tool_name": call.tool_name,
                    "status": call.status,
                    "policy_decision": call.policy_decision,
                    "replay_policy": call.replay_policy,
                    "side_effect_level": call.side_effect_level,
                    "replayed": bool(call.replayed),
                    "result_summary": call.result_summary,
                    "created_at": _iso(call.created_at),
                }
                for call in calls
            ],
        }

    def delta(
        self,
        *,
        task_id: str,
        tenant_id: str,
        after_event_id: str | None,
        limit: int = 200,
    ) -> dict:
        task_service = TaskService(self.session)
        task = task_service.get(task_id, tenant_id=tenant_id)
        query = self.session.query(AnalysisEvent).filter(AnalysisEvent.task_id == task_id)
        previous_count = 0
        if after_event_id:
            anchor = self.session.get(AnalysisEvent, after_event_id)
            if anchor is not None and anchor.task_id == task_id:
                previous_count = int(
                    query.with_entities(func.count(AnalysisEvent.id))
                    .filter(
                        or_(
                            AnalysisEvent.created_at < anchor.created_at,
                            and_(
                                AnalysisEvent.created_at == anchor.created_at,
                                AnalysisEvent.id <= anchor.id,
                            ),
                        )
                    )
                    .scalar()
                    or 0
                )
                query = query.filter(
                    or_(
                        AnalysisEvent.created_at > anchor.created_at,
                        and_(
                            AnalysisEvent.created_at == anchor.created_at,
                            AnalysisEvent.id > anchor.id,
                        ),
                    )
                )
        events = (
            query.order_by(AnalysisEvent.created_at.asc(), AnalysisEvent.id.asc())
            .limit(limit)
            .all()
        )
        items = [
            _event_out(event, event_seq=previous_count + index)
            for index, event in enumerate(events, start=1)
        ]
        task_out = task_service.to_out(task)
        if not task_out.get("current_phase"):
            task_out["current_phase"] = task_phase_for_node(task.current_node).code
        return {
            "task": task_out,
            "state_revision": int(task.state_revision or 0),
            "event_cursor": items[-1]["id"] if items else after_event_id,
            "events": items,
        }


def sse_frame(event: str, data: dict, *, event_id: str | None = None) -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(
        "data: "
        + json.dumps(
            jsonable_encoder(data),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return "\n".join(lines) + "\n\n"


def task_event_stream(
    *,
    task_id: str,
    tenant_id: str,
    max_seconds: int = 60,
    poll_seconds: float = 0.25,
) -> Iterator[str]:
    """Emit a fresh snapshot first, then append-only deltas until terminal or timeout."""
    from app.db import session as db_session

    started = time.monotonic()
    session = db_session.SessionLocal()
    try:
        snapshot = TaskLiveService(session).snapshot(task_id=task_id, tenant_id=tenant_id)
    finally:
        session.close()
    cursor = snapshot.get("event_cursor")
    yield sse_frame("snapshot", snapshot, event_id=cursor)

    status = (snapshot.get("task") or {}).get("status")
    if status in TERMINAL_STATUSES:
        yield sse_frame(
            "done",
            {"task_id": task_id, "status": status, "state_revision": snapshot["state_revision"]},
            event_id=cursor,
        )
        return
    if status == TaskStatus.WAITING_HUMAN.value:
        yield sse_frame(
            "paused",
            {"task_id": task_id, "status": status, "state_revision": snapshot["state_revision"]},
            event_id=cursor,
        )
        return

    last_heartbeat = time.monotonic()
    while time.monotonic() - started < max_seconds:
        time.sleep(poll_seconds)
        session = db_session.SessionLocal()
        try:
            delta = TaskLiveService(session).delta(
                task_id=task_id,
                tenant_id=tenant_id,
                after_event_id=cursor,
            )
        finally:
            session.close()
        for item in delta["events"]:
            cursor = item["id"]
            yield sse_frame(
                "delta",
                {
                    "task_id": task_id,
                    "state_revision": delta["state_revision"],
                    "event": item,
                },
                event_id=cursor,
            )
        status = delta["task"]["status"]
        if status in TERMINAL_STATUSES:
            yield sse_frame(
                "done",
                {"task_id": task_id, "status": status, "state_revision": delta["state_revision"]},
                event_id=cursor,
            )
            return
        if status == TaskStatus.WAITING_HUMAN.value:
            yield sse_frame(
                "paused",
                {"task_id": task_id, "status": status, "state_revision": delta["state_revision"]},
                event_id=cursor,
            )
            return
        if time.monotonic() - last_heartbeat >= 10:
            yield sse_frame(
                "heartbeat",
                {"task_id": task_id, "state_revision": delta["state_revision"]},
                event_id=cursor,
            )
            last_heartbeat = time.monotonic()

    yield sse_frame("timeout", {"task_id": task_id}, event_id=cursor)


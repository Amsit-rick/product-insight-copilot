"""Task mutations use atomic state revisions instead of last-write-wins updates."""

from __future__ import annotations

import pytest

from app.db.session import build_session_factory
from app.graph.runtime import NodeRuntime
from app.models.enums import TaskStatus
from app.models.workflow import AnalysisEvent, AnalysisTask
from app.services.task_service import TaskRevisionConflict, TaskService
from app.services.workflow_runner import WorkflowRunner


def _task(session, *, status: str, revision: int) -> AnalysisTask:
    row = AnalysisTask(
        tenant_id="tenant-revision",
        user_id="user-revision",
        title="revision control",
        query="verify optimistic concurrency control",
        status=status,
        state_revision=revision,
    )
    session.add(row)
    session.commit()
    return row


def test_stale_cancel_is_rejected_before_any_side_effect(session) -> None:
    task = _task(session, status=TaskStatus.RUNNING.value, revision=3)
    service = TaskService(session)

    with pytest.raises(TaskRevisionConflict) as excinfo:
        service.request_cancel(
            task.id,
            tenant_id=task.tenant_id,
            expected_revision=2,
        )

    assert excinfo.value.expected_revision == 2
    assert excinfo.value.actual_revision == 3
    session.refresh(task)
    assert task.cancel_requested is False
    assert session.query(AnalysisEvent).filter(AnalysisEvent.task_id == task.id).count() == 0


def test_successful_cancel_claims_revision_once_and_rejects_duplicate(session) -> None:
    task = _task(session, status=TaskStatus.RUNNING.value, revision=4)
    service = TaskService(session)

    updated = service.request_cancel(
        task.id,
        tenant_id=task.tenant_id,
        expected_revision=4,
    )
    session.commit()

    assert updated.state_revision == 5
    assert updated.cancel_requested is True
    with pytest.raises(TaskRevisionConflict) as excinfo:
        service.request_cancel(
            task.id,
            tenant_id=task.tenant_id,
            expected_revision=4,
        )
    assert excinfo.value.actual_revision == 5
    assert session.query(AnalysisEvent).filter(AnalysisEvent.task_id == task.id).count() == 1


def test_retry_claims_revision_exactly_once(session) -> None:
    task = _task(session, status=TaskStatus.FAILED.value, revision=7)

    updated = TaskService(session).prepare_retry(
        task.id,
        tenant_id=task.tenant_id,
        reset_retry_count=False,
        expected_revision=7,
    )

    assert updated.status == TaskStatus.PENDING.value
    assert updated.state_revision == 8
    assert updated.retry_count == 1


def test_delayed_resume_cannot_reuse_an_already_consumed_revision(engine) -> None:
    factory = build_session_factory(engine)
    session = factory()
    try:
        task = _task(session, status=TaskStatus.WAITING_HUMAN.value, revision=10)
        task_id = task.id
    finally:
        session.close()

    runtime = NodeRuntime(session_factory=factory)
    runtime.record_resume(
        task_id,
        resumed_from_checkpoint=True,
        wake_reason="human_approval",
        expected_revision=10,
    )

    with pytest.raises(TaskRevisionConflict) as excinfo:
        runtime.record_resume(
            task_id,
            resumed_from_checkpoint=True,
            wake_reason="duplicate_resume",
            expected_revision=10,
        )
    assert excinfo.value.actual_revision == 11


def test_two_sessions_that_loaded_the_same_revision_cannot_both_cancel(engine) -> None:
    factory = build_session_factory(engine)
    seed = factory()
    try:
        task = _task(seed, status=TaskStatus.RUNNING.value, revision=0)
        task_id = task.id
    finally:
        seed.close()

    first = factory()
    second = factory()
    try:
        assert TaskService(first).get(task_id).state_revision == 0
        assert TaskService(second).get(task_id).state_revision == 0

        TaskService(first).request_cancel(task_id, expected_revision=0)
        first.commit()

        with pytest.raises(TaskRevisionConflict) as excinfo:
            TaskService(second).request_cancel(task_id, expected_revision=0)
        assert excinfo.value.actual_revision == 1
    finally:
        first.close()
        second.close()


def test_workflow_runner_treats_stale_resume_as_consumed_message() -> None:
    events: list[dict] = []

    class RuntimeStub:
        def record_resume(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            raise TaskRevisionConflict(expected_revision=2, actual_revision=3)

        def emit_event(self, task_id, node, message, payload, **kwargs):  # noqa: ANN001
            events.append(
                {
                    "task_id": task_id,
                    "node": node,
                    "message": message,
                    "payload": payload,
                    "level": kwargs.get("level"),
                }
            )

    runner = WorkflowRunner.__new__(WorkflowRunner)
    runner.runtime = RuntimeStub()

    claimed = runner._record_resume(
        "task-stale-resume",
        resumed_from_checkpoint=True,
        wake_reason="human_approval",
        expected_revision=2,
    )

    assert claimed is False
    assert events[0]["message"] == "恢复请求已过期，已安全跳过"
    assert events[0]["payload"] == {"expected_revision": 2, "actual_revision": 3}


"""Mutable task APIs reject stale browser and reviewer state."""

from __future__ import annotations

from app.db.session import SessionLocal
from app.models.enums import TaskStatus
from app.models.identity import User
from app.models.workflow import AnalysisTask, Approval
from app.services.task_execution import TaskExecutionService


def _seed_task(*, status: str, revision: int) -> tuple[str, str | None]:
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.username == "pm").one()
        task = AnalysisTask(
            tenant_id=user.tenant_id,
            user_id=user.id,
            title="revision api",
            query="verify stale mutations are rejected",
            status=status,
            state_revision=revision,
        )
        session.add(task)
        session.flush()
        TaskExecutionService(session).ensure_root(task)
        approval_id = None
        if status == TaskStatus.WAITING_HUMAN.value:
            approval = Approval(task_id=task.id, status="pending")
            session.add(approval)
            session.flush()
            approval_id = approval.id
        session.commit()
        return task.id, approval_id
    finally:
        session.close()


def _assert_revision_conflict(response, *, expected: int, actual: int) -> None:  # noqa: ANN001
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail == {
        "code": "task_revision_conflict",
        "message": f"任务版本冲突：期望 {expected}，实际 {actual}，请刷新后重试",
        "expected_revision": expected,
        "actual_revision": actual,
    }


def test_cancel_api_rejects_stale_revision_and_returns_current_revision(client, auth) -> None:
    task_id, _ = _seed_task(status=TaskStatus.RUNNING.value, revision=5)

    stale = client.post(
        f"/api/v1/tasks/{task_id}/cancel",
        headers=auth["pm"],
        json={"expected_revision": 4},
    )
    _assert_revision_conflict(stale, expected=4, actual=5)

    accepted = client.post(
        f"/api/v1/tasks/{task_id}/cancel",
        headers=auth["pm"],
        json={"expected_revision": 5},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state_revision"] == 6


def test_retry_api_does_not_dispatch_when_revision_is_stale(client, auth, monkeypatch) -> None:
    task_id, _ = _seed_task(status=TaskStatus.FAILED.value, revision=8)
    dispatched: list[str] = []
    monkeypatch.setattr(
        "app.api.routes_tasks.dispatch_task",
        lambda task_id, **_kwargs: dispatched.append(task_id) or "inline",
    )

    stale = client.post(
        f"/api/v1/tasks/{task_id}/retry",
        headers=auth["pm"],
        json={"reason": "stale browser", "expected_revision": 7},
    )

    _assert_revision_conflict(stale, expected=7, actual=8)
    assert dispatched == []


def test_task_approval_does_not_resume_when_revision_is_stale(client, auth, monkeypatch) -> None:
    task_id, _ = _seed_task(status=TaskStatus.WAITING_HUMAN.value, revision=11)
    dispatched: list[str] = []
    monkeypatch.setattr(
        "app.api.routes_tasks.dispatch_task",
        lambda task_id, **_kwargs: dispatched.append(task_id) or "inline",
    )

    stale = client.post(
        f"/api/v1/tasks/{task_id}/approve",
        headers=auth["reviewer"],
        json={"action": "approve", "expected_revision": 10},
    )

    _assert_revision_conflict(stale, expected=10, actual=11)
    assert dispatched == []


def test_approval_record_endpoint_uses_the_same_revision_guard(client, auth, monkeypatch) -> None:
    task_id, approval_id = _seed_task(status=TaskStatus.WAITING_HUMAN.value, revision=13)

    class FakeRunner:
        def resume(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("stale approval must not resume")

    monkeypatch.setattr("app.api.routes_approvals.get_runner", lambda: FakeRunner())
    stale = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        headers=auth["reviewer"],
        json={"action": "approve", "expected_revision": 12},
    )

    _assert_revision_conflict(stale, expected=12, actual=13)
    session = SessionLocal()
    try:
        assert session.get(AnalysisTask, task_id).state_revision == 13
        assert session.get(Approval, approval_id).status == "pending"
    finally:
        session.close()

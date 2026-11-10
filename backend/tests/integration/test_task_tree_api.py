"""The task tree API reflects real Agent, tool and approval execution units."""

from __future__ import annotations


def _create_forced_review_task() -> str:
    from app.db import session as db_session
    from app.models.identity import User
    from app.services.task_service import TaskService

    session = db_session.SessionLocal()
    try:
        user = session.query(User).filter(User.username == "pm").one()
        task, created = TaskService(session).create(
            user=user,
            query="Android 3.2.0 发布后留存下降，请核对版本、反馈与指标证据。",
            filters={
                "product": "星野社区",
                "platform": "Android",
                "app_version": "3.2.0",
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
                "knowledge_bases": [
                    "product_kb",
                    "feedback_kb",
                    "engineering_kb",
                    "operation_kb",
                ],
                "metrics": ["next_day_retention", "crash_rate"],
            },
            title="task tree integration",
            force_human_review=True,
        )
        assert created is True
        session.commit()
        return task.id
    finally:
        session.close()


def test_tree_api_tracks_real_agent_tool_and_approval_lifecycle(client, auth) -> None:
    from app.services.workflow_runner import get_runner

    task_id = _create_forced_review_task()
    result = get_runner().run(task_id)
    assert result["status"] == "waiting_human"

    waiting = client.get(f"/api/v1/tasks/{task_id}/tree", headers=auth["pm"])
    assert waiting.status_code == 200, waiting.text
    tree = waiting.json()
    assert tree["unit_type"] == "workflow"
    assert tree["status"] == "waiting"

    agent_units = [item for item in tree["children"] if item["unit_type"] == "agent"]
    approval_units = [item for item in tree["children"] if item["unit_type"] == "approval"]
    assert len(agent_units) >= 4
    assert approval_units and approval_units[-1]["status"] == "waiting"
    assert any(
        child["unit_type"] == "tool"
        for agent in agent_units
        for child in agent["children"]
    )

    approved = client.post(
        f"/api/v1/tasks/{task_id}/approve",
        headers=auth["reviewer"],
        json={"action": "approve", "comment": "task tree lifecycle integration"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "completed"

    completed = client.get(f"/api/v1/tasks/{task_id}/tree", headers=auth["pm"]).json()
    assert completed["status"] == "succeeded"
    approval_units = [item for item in completed["children"] if item["unit_type"] == "approval"]
    assert approval_units[-1]["status"] == "succeeded"


def test_task_snapshot_embeds_the_same_persistent_tree(client, auth) -> None:
    from app.services.workflow_runner import get_runner

    task_id = _create_forced_review_task()
    get_runner().run(task_id)

    tree = client.get(f"/api/v1/tasks/{task_id}/tree", headers=auth["pm"]).json()
    snapshot = client.get(f"/api/v1/tasks/{task_id}/snapshot", headers=auth["pm"]).json()
    assert snapshot["task_tree"] == tree


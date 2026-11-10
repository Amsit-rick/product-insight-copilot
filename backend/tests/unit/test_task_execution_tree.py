"""Persistent parent/child execution units and ownership policies."""

from __future__ import annotations

from app.models.workflow import AnalysisTask
from app.services.task_execution import TaskExecutionService


def _task(session) -> AnalysisTask:  # noqa: ANN001
    row = AnalysisTask(
        tenant_id="tenant-task-tree",
        user_id="user-task-tree",
        title="task tree",
        query="verify persistent task ownership tree",
    )
    session.add(row)
    session.flush()
    return row


def test_tree_persists_owner_relationships_and_is_idempotent(session) -> None:
    task = _task(session)
    service = TaskExecutionService(session)
    root = service.ensure_root(task)
    agent = service.start_unit(
        task_id=task.id,
        name="订单事实 Agent",
        unit_type="agent",
        owner_type="agent_run",
        owner_ref_id="agent-run-001",
        parent_unit_id=root.id,
        failure_policy="continue",
    )
    same_agent = service.start_unit(
        task_id=task.id,
        name="订单事实 Agent duplicate",
        unit_type="agent",
        owner_type="agent_run",
        owner_ref_id="agent-run-001",
        parent_unit_id=root.id,
    )
    tool = service.start_unit(
        task_id=task.id,
        name="查询订单事实",
        unit_type="tool",
        owner_type="tool_call",
        owner_ref_id="tool-call-001",
        parent_unit_id=agent.id,
    )

    assert same_agent.id == agent.id
    tree = service.tree(task.id)
    assert tree["id"] == root.id
    assert tree["owner_type"] == "workflow"
    assert tree["children"][0]["id"] == agent.id
    assert tree["children"][0]["children"][0]["id"] == tool.id


def test_parent_cancellation_propagates_only_to_attached_units(session) -> None:
    task = _task(session)
    service = TaskExecutionService(session)
    root = service.ensure_root(task)
    attached = service.start_unit(
        task_id=task.id,
        name="附属 Agent",
        unit_type="agent",
        owner_type="agent_run",
        owner_ref_id="agent-attached",
        parent_unit_id=root.id,
    )
    independent = service.start_unit(
        task_id=task.id,
        name="独立告警投递",
        unit_type="background",
        owner_type="system",
        owner_ref_id="background-001",
        parent_unit_id=root.id,
        execution_mode="background",
        cancellation_policy="independent",
        failure_policy="continue",
    )

    affected = service.request_cancel_descendants(task.id, reason="parent_cancelled")
    session.flush()
    session.refresh(attached)
    session.refresh(independent)

    assert attached.id in affected
    assert attached.status == "cancel_requested"
    assert independent.id not in affected
    assert independent.status == "running"


def test_child_failure_policy_is_explicit_and_deterministic(session) -> None:
    task = _task(session)
    service = TaskExecutionService(session)
    root = service.ensure_root(task)
    blocking = service.start_unit(
        task_id=task.id,
        name="人工审批",
        unit_type="approval",
        owner_type="approval",
        owner_ref_id="approval-block",
        parent_unit_id=root.id,
        failure_policy="block_parent",
    )
    service.finish_unit(blocking.id, status="failed", error="review unavailable")
    session.flush()
    session.refresh(root)
    assert root.status == "waiting"
    assert root.metadata_json["blocked_by_unit_id"] == blocking.id

    fatal = service.start_unit(
        task_id=task.id,
        name="关键子任务",
        unit_type="agent",
        owner_type="agent_run",
        owner_ref_id="agent-fatal",
        parent_unit_id=root.id,
        failure_policy="fail_parent",
    )
    service.finish_unit(fatal.id, status="failed", error="fatal child failure")
    session.flush()
    session.refresh(root)
    assert root.status == "failed"
    assert root.metadata_json["failed_by_unit_id"] == fatal.id


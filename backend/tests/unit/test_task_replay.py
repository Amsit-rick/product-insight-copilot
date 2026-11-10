"""任务级确定性回放快照。"""

from __future__ import annotations

from app.models.workflow import AgentRun, AnalysisEvent, AnalysisTask, Approval, ToolCall
from app.services.task_execution import TaskExecutionService
from app.services.task_replay import TaskReplayService


def _seed_task(session, *, task_id: str | None = None) -> AnalysisTask:
    task = AnalysisTask(
        id=task_id,
        tenant_id="tenant-replay",
        user_id="user-replay",
        title="回放测试",
        query="查询订单 13800138000 的处理依据",
        filters={"knowledge_bases": ["policy"]},
        status="completed",
        workflow_version="workflow-v1",
        prompt_version="prompt-v1",
        result={"report_id": "report-1"},
    )
    session.add(task)
    session.flush()
    session.add(
        AnalysisEvent(
            task_id=task.id,
            node="finalize",
            message="任务完成",
            payload={"phone": "13800138000"},
        )
    )
    session.add(
        ToolCall(
            task_id=task.id,
            tool_name="query_order_facts",
            arguments={"order_no": "ORDER-1"},
            result_summary="返回 1 行",
            result={
                "rows": [{"receiver_phone": "13800138000", "paid_amount": 89}],
                "access_token": "top-secret-token",
            },
            result_digest="source-digest",
            status="success",
            policy_decision="allow",
        )
    )
    session.commit()
    return task


def test_创建回放快照时完整收集并脱敏工具结果(session):
    task = _seed_task(session)

    snapshot, created = TaskReplayService(session).create_snapshot(
        task_id=task.id,
        tenant_id=task.tenant_id,
        created_by="reviewer-1",
    )

    assert created is True
    assert snapshot.schema_version == "task-replay.v1"
    assert len(snapshot.digest) == 64
    assert snapshot.package["mode"] == "playback"
    assert snapshot.package["playback_fidelity"] == "complete"
    assert snapshot.package["completeness"]["missing_tool_results"] == 0
    assert snapshot.package["task"]["query"] == "查询订单 138****8000 的处理依据"
    tool_result = snapshot.package["tool_calls"][0]["result"]
    assert tool_result["rows"][0]["receiver_phone"] == "138****8000"
    assert tool_result["access_token"] != "top-secret-token"
    assert "top-secret-token" not in str(snapshot.package)


def test_回放包会为历史任务补齐根执行单元(session):
    """旧任务可能没有执行树；回放包不能因此缺 workflow 根单元。"""

    task = _seed_task(session)

    package = TaskReplayService(session).build_package(
        task_id=task.id,
        tenant_id=task.tenant_id,
    )

    roots = [
        unit
        for unit in package["execution_units"]
        if unit["owner_type"] == "workflow" and unit["owner_ref_id"] == task.id
    ]
    assert roots, "回放包必须包含 workflow 根执行单元"
    assert roots[0]["id"] == task.id


def test_回放包不会把含手机号形态数字的UUID误脱敏(session):
    task_id = "2deb36ce-57e1-468b-a852-b05929371753"
    task = _seed_task(session, task_id=task_id)

    package = TaskReplayService(session).build_package(
        task_id=task.id,
        tenant_id=task.tenant_id,
    )

    root = next(
        unit for unit in package["execution_units"] if unit["owner_type"] == "workflow"
    )
    assert root["id"] == task_id
    assert root["owner_ref_id"] == task_id


def test_同一任务状态重复生成快照保持幂等(session):
    task = _seed_task(session)
    service = TaskReplayService(session)

    first, first_created = service.create_snapshot(
        task_id=task.id,
        tenant_id=task.tenant_id,
        created_by="reviewer-1",
    )
    second, second_created = service.create_snapshot(
        task_id=task.id,
        tenant_id=task.tenant_id,
        created_by="reviewer-2",
    )

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    assert second.digest == first.digest


def test_回放包被篡改后校验失败(session):
    task = _seed_task(session)
    service = TaskReplayService(session)
    snapshot, _ = service.create_snapshot(
        task_id=task.id,
        tenant_id=task.tenant_id,
        created_by="reviewer-1",
    )

    assert service.verify_snapshot(snapshot).valid is True
    snapshot.package["task"]["query"] = "已被篡改"

    verification = service.verify_snapshot(snapshot)

    assert verification.valid is False
    assert verification.expected_digest == snapshot.digest
    assert verification.actual_digest != snapshot.digest


def test_历史工具结果缺失时必须标记为部分回放(session):
    task = _seed_task(session)
    session.add(
        ToolCall(
            task_id=task.id,
            tool_name="legacy_tool",
            arguments={},
            result_summary="旧记录只有摘要",
            result=None,
            result_digest=None,
            status="success",
            policy_decision="allow",
        )
    )
    session.commit()

    snapshot, _ = TaskReplayService(session).create_snapshot(
        task_id=task.id,
        tenant_id=task.tenant_id,
        created_by="reviewer-1",
    )

    assert snapshot.package["playback_fidelity"] == "partial"
    assert snapshot.package["completeness"] == {
        "tool_results_total": 2,
        "tool_results_captured": 1,
        "missing_tool_results": 1,
    }


def test_回放包包含任务执行树及Agent工具审批所有权(session):
    task = _seed_task(session)
    agent_run = AgentRun(
        task_id=task.id,
        agent_code="knowledge_agent",
        status="succeeded",
    )
    approval = Approval(
        task_id=task.id,
        status="approved",
        action="publish_report",
    )
    session.add_all([agent_run, approval])
    session.flush()
    tool_call = session.query(ToolCall).filter(ToolCall.task_id == task.id).one()

    execution = TaskExecutionService(session)
    root = execution.ensure_root(task)
    agent_unit = execution.start_unit(
        task_id=task.id,
        name="知识检索 Agent",
        unit_type="agent",
        owner_type="agent_run",
        owner_ref_id=agent_run.id,
        metadata={"phase": "evidence_collection"},
    )
    execution.start_unit(
        task_id=task.id,
        parent_unit_id=agent_unit.id,
        name="订单事实查询",
        unit_type="tool",
        owner_type="tool_call",
        owner_ref_id=tool_call.id,
    )
    execution.start_unit(
        task_id=task.id,
        parent_unit_id=root.id,
        name="报告发布审批",
        unit_type="approval",
        owner_type="approval",
        owner_ref_id=approval.id,
        status="succeeded",
    )
    session.commit()

    snapshot, created = TaskReplayService(session).create_snapshot(
        task_id=task.id,
        tenant_id=task.tenant_id,
        created_by="reviewer-1",
    )

    assert created is True
    package = snapshot.package
    units = package["execution_units"]
    owners = {(row["owner_type"], row["owner_ref_id"]): row for row in units}
    assert owners[("workflow", task.id)]["id"] == task.id
    assert owners[("agent_run", agent_run.id)]["parent_unit_id"] == task.id
    assert owners[("agent_run", agent_run.id)]["metadata"] == {
        "phase": "evidence_collection"
    }
    assert owners[("tool_call", tool_call.id)]["parent_unit_id"] == agent_unit.id
    assert owners[("approval", approval.id)]["parent_unit_id"] == task.id


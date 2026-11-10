"""Task phase and recovery metadata remain durable across runtime transitions."""

from __future__ import annotations

from app.db.session import build_session_factory
from app.graph.runtime import NodeRuntime
from app.models.workflow import AnalysisTask
from app.services.task_runtime import task_phase_for_node


def test_business_phase_mapping_is_stable_and_user_facing() -> None:
    assert task_phase_for_node("validate_request").code == "intake"
    assert task_phase_for_node("product_agent").code == "fact_gathering"
    assert task_phase_for_node("reflect_report").code == "reflection"
    assert task_phase_for_node("human_approval").code == "human_approval"
    assert task_phase_for_node("persist_result").code == "completed"


def test_runtime_persists_phase_attempt_checkpoint_and_resume_metadata(engine) -> None:
    factory = build_session_factory(engine)
    session = factory()
    try:
        task = AnalysisTask(
            tenant_id="tenant-durable-state",
            user_id="user-durable-state",
            title="durable state",
            query="verify durable task state",
        )
        session.add(task)
        session.commit()
        task_id = task.id
    finally:
        session.close()

    runtime = NodeRuntime(session_factory=factory)
    state = {"task_id": task_id, "tenant_id": "tenant-durable-state"}
    with runtime.node_session(state, "validate_request"):
        pass
    with runtime.node_session(state, "product_agent"):
        pass
    with runtime.node_session(state, "product_agent"):
        pass
    with runtime.node_session(state, "reflect_report", load_task=False):
        pass
    runtime.record_resume(
        task_id,
        resumed_from_checkpoint=True,
        wake_reason="human_approval",
    )

    session = factory()
    try:
        stored = session.get(AnalysisTask, task_id)
        assert stored.current_node == "reflect_report"
        assert stored.current_phase == "reflection"
        assert stored.phase_attempt == 1
        assert stored.checkpoint_version >= 4
        assert stored.state_revision >= stored.checkpoint_version
        assert stored.resume_count == 1
        assert stored.resumed_from_checkpoint is True
        assert stored.wake_reason == "human_approval"
    finally:
        session.close()


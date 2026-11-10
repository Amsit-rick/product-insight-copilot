"""回归：状态写入不得在节点会话持锁时另开连接（会变 `database is locked`）。"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.session import build_session_factory
from app.graph.runtime import NodeRuntime
from app.models.enums import TaskStatus
from app.models.workflow import AnalysisTask


@pytest.fixture()
def runtime(engine: Engine) -> NodeRuntime:
    """运行时指向临时库（绝不能落到开发库 backend/data/pic.db）。"""
    return NodeRuntime(session_factory=build_session_factory(engine))


@pytest.fixture()
def task_state(runtime: NodeRuntime) -> dict:
    session = runtime.session_factory()
    try:
        row = AnalysisTask(
            tenant_id="t-status",
            user_id="u-status",
            query="状态写入回归",
            title="状态写入回归",
        )
        session.add(row)
        session.commit()
        return {"task_id": row.id, "tenant_id": row.tenant_id, "user_id": row.user_id}
    finally:
        session.close()


def test_wait_for_human_reuses_node_session(runtime: NodeRuntime, task_state: dict) -> None:
    """节点会话里已有未提交写入时，wait_for_human 必须复用同一连接。

    旧实现用第二个连接改 `analysis_tasks`，SQLite 下会自己锁自己：
    轻则等满 30 秒 busy_timeout（整套用例超时），重则直接 `database is locked`，
    任务被判 failed —— 注入/审批用例都因此挂过。
    """
    opened: list[Session] = []
    real_factory = runtime.session_factory

    def counting_factory() -> Session:
        session = real_factory()
        opened.append(session)
        return session

    runtime.session_factory = counting_factory

    with runtime.node_session(task_state, "human_approval") as ctx:
        ctx.emit("审批前写入事件（故意不提交，模拟持有写锁）")
        assert len(opened) == 1, "节点会话之外不得再开连接"

        ctx.wait_for_human("问题文本包含提示词注入特征，必须人工确认")
        assert len(opened) == 1, "wait_for_human 不得另开写连接，必须复用节点会话"

        # 立即提交：写锁不能拖到人工等待期间
        check = real_factory()
        try:
            stored = check.get(AnalysisTask, task_state["task_id"])
            assert stored.status == TaskStatus.WAITING_HUMAN.value
            assert stored.current_node == "human_approval"
            assert "注入" in (stored.waiting_reason or "")
        finally:
            check.close()

    check = real_factory()
    try:
        stored = check.get(AnalysisTask, task_state["task_id"])
        assert stored.status == TaskStatus.WAITING_HUMAN.value, "节点退出提交不得覆盖人工等待状态"
    finally:
        check.close()


def test_touch_and_resume_write_through(runtime: NodeRuntime, task_state: dict) -> None:
    with runtime.node_session(task_state, "plan") as ctx:
        ctx.touch(status=TaskStatus.RUNNING)
    with runtime.node_session(task_state, "human_approval") as ctx:
        ctx.wait_for_human("需人工确认")
        ctx.resume_from_human()

    session = runtime.session_factory()
    try:
        stored = session.get(AnalysisTask, task_state["task_id"])
        assert stored.status == TaskStatus.RUNNING.value
        assert stored.waiting_reason == ""
        assert stored.current_node == "human_approval"
        assert stored.started_at is not None, "首次进入 running 应记录开始时间"
    finally:
        session.close()

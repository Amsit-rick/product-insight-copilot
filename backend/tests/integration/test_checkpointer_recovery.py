"""集成测试：断点恢复能不能跨进程/跨副本。

这条能力只有"真的换一个 runner 实例"才能验证：
- `memory`：中断状态在进程内存里 → 新实例拿不到 checkpoint，只能走"用落库摘要降级重建报告"；
- `sqlite`：checkpointer 落在共享文件里 → 新实例能拿到 checkpoint，正常恢复工作流。

所以这里先用 sqlite 做一次完整的"跑→中断→换实例→恢复"，再用 memory 对照，
把"为什么生产必须换掉默认值"变成可执行的证据，而不是文档里的一句话。
"""

from __future__ import annotations

import gc
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.config import get_settings
from app.db.session import SessionLocal
from app.graph.checkpointer import (
    CheckpointerConfigError,
    build_checkpointer,
    describe_checkpointer,
    reset_checkpointer,
)
from app.models.identity import User
from app.models.workflow import AnalysisTask
from app.services.task_service import TaskService
from app.services.workflow_runner import WorkflowRunner

QUESTION = "Android 端 3.2.0 发布后次日留存下降，原因是什么？"
FILTERS = {
    "product": "星野社区",
    "platform": "Android",
    "app_version": "3.2.0",
    "date_from": "2026-08-01",
    "date_to": "2026-08-31",
    "knowledge_bases": ["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
}


def make_task() -> str:
    session = SessionLocal()
    try:
        pm = session.query(User).filter(User.username == "pm").first()
        task, _ = TaskService(session).create(user=pm, query=QUESTION, filters=FILTERS)
        session.commit()
        return task.id
    finally:
        session.close()


def task_status(task_id: str) -> str:
    session = SessionLocal()
    try:
        return session.get(AnalysisTask, task_id).status
    finally:
        session.close()


def settings_with_checkpointer(tmp_path: Path, backend: str):
    path = tmp_path / "checkpoints.db"
    return get_settings().model_copy(
        update={
            "checkpointer_backend": backend,
            "checkpointer_sqlite_path": str(path),
        }
    )


def make_runner(settings) -> WorkflowRunner:
    """构造一个独立 runner（模拟"另一个副本/重启后的进程"）。

    `build_checkpointer()` 在**进程内是单例**（一个进程一个 saver，这是有意的：
    避免每个 runner 各开一份连接）。所以要模拟"另一个进程"，必须先把单例清掉再构建——
    否则拿到的还是同一个对象，测出来的"恢复成功"就只是因为状态本来就在内存里。
    """
    return WorkflowRunner(settings=settings, checkpointer=build_checkpointer(settings))


def make_runner_in_new_process(settings) -> WorkflowRunner:
    """清掉进程内单例后重新构建 = 模拟另一个进程/副本。"""
    reset_checkpointer()
    return WorkflowRunner(settings=settings, checkpointer=build_checkpointer(settings))


@pytest.fixture(autouse=True)
def _reset_saver():
    reset_checkpointer()
    yield
    reset_checkpointer()


def test_sqlite_checkpointer_survives_new_runner_instance(client, tmp_path):
    """sqlite checkpointer：换一个 runner 实例仍然能恢复（多副本/重启可用）。"""
    settings = settings_with_checkpointer(tmp_path, "sqlite")

    task_id = make_task()
    first = make_runner(settings)
    first.run(task_id)
    assert task_status(task_id) == "waiting_human", "该问题应进入人工审核分支"

    # 关键：模拟进程重启/换副本 —— 全新 runner + 全新 saver，只有 checkpointer 文件是共同点
    second = make_runner_in_new_process(settings)
    assert second.checkpointer is not first.checkpointer
    second.resume(task_id, {"action": "approve", "comment": "跨实例恢复验证", "reviewer_id": None})

    assert task_status(task_id) == "completed", "共享 checkpointer 下应正常恢复并出报告"


def test_memory_checkpointer_has_no_state_in_new_instance(client, tmp_path):
    """对照：memory checkpointer 在新实例里没有 checkpoint —— 这正是单副本的隐患。"""
    settings = settings_with_checkpointer(tmp_path, "memory")

    task_id = make_task()
    make_runner(settings).run(task_id)
    assert task_status(task_id) == "waiting_human"

    fresh = make_runner_in_new_process(settings)
    config = {"configurable": {"thread_id": task_id}}
    assert fresh.checkpointer.get_tuple(config) is None, "内存 checkpointer 不该在新实例里有状态"

    # 即便没有 checkpoint，系统仍要能交付：走落库摘要的降级恢复路径
    fresh.resume(task_id, {"action": "approve", "comment": "降级恢复", "reviewer_id": None})
    assert task_status(task_id) == "completed"


def test_sqlite_checkpointer_describe_reports_shared(tmp_path):
    settings = settings_with_checkpointer(tmp_path, "sqlite")
    build_checkpointer(settings)  # 先构建，描述里才会有实际路径

    described = describe_checkpointer(settings)

    assert described["backend"] == "sqlite"
    assert described["shared"] is True
    assert "checkpoints.db" in described["path"]


@pytest.mark.parametrize("backend", ["sqlite", "memory"])
def test_health_exposes_checkpointer_and_execution(backend, tmp_path):
    """运维要能一眼看出"当前恢复是不是跨进程共享"——不然会误以为多副本没问题。"""
    settings = settings_with_checkpointer(tmp_path, backend)
    reset_checkpointer()
    build_checkpointer(settings)

    described = describe_checkpointer(settings)

    assert described["backend"] == backend
    assert described["shared"] is (backend != "memory")
    assert described["note"]


def test_unknown_backend_fails_fast(tmp_path):
    settings = get_settings().model_copy(update={"checkpointer_backend": "redis"})

    with pytest.raises(CheckpointerConfigError) as excinfo:
        build_checkpointer(settings)

    assert "不支持" in str(excinfo.value)


def test_postgres_backend_without_dependency_gives_actionable_error(tmp_path):
    """配置了 postgres 但没装依赖：报错必须说清"装什么 / 或者改用什么"。

    这里不断言"一定抛错"——如果环境里恰好装了 langgraph-checkpoint-postgres，
    就应该走"缺连接串"那条分支；两种都算配置错误，都必须给出可执行的下一步。
    """
    settings = get_settings().model_copy(
        update={"checkpointer_backend": "postgres", "checkpointer_postgres_url": "", "database_url": "sqlite:///x.db"}
    )

    with pytest.raises(CheckpointerConfigError) as excinfo:
        build_checkpointer(settings)

    message = str(excinfo.value)
    assert "postgres" in message or "langgraph-checkpoint-postgres" in message


def test_postgres_checkpointer_keeps_connection_context_alive(monkeypatch):
    """PostgresSaver 的 context manager 必须与进程单例同寿命。

    ``from_conn_string()`` 返回上下文管理器；若只对临时对象调用 ``__enter__``，
    临时对象被回收后会关闭连接，worker 首次真正执行就报 ``connection is closed``。
    """

    class FakeSaver:
        def __init__(self) -> None:
            self.closed = False
            self.setup_called = False

        def setup(self) -> None:
            self.setup_called = True

    contexts = []

    class FakeContext:
        def __init__(self) -> None:
            self.saver = FakeSaver()
            self.exited = False
            contexts.append(self)

        def __enter__(self):
            return self.saver

        def __exit__(self, *_args):
            self.exited = True
            self.saver.closed = True

        def __del__(self):
            if not self.exited:
                self.saver.closed = True

    class FakePostgresSaver:
        @classmethod
        def from_conn_string(cls, _url):
            return FakeContext()

    fake_module = ModuleType("langgraph.checkpoint.postgres")
    fake_module.PostgresSaver = FakePostgresSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", fake_module)
    settings = get_settings().model_copy(
        update={
            "checkpointer_backend": "postgres",
            "checkpointer_postgres_url": "postgresql://example.invalid/test",
        }
    )

    saver = build_checkpointer(settings)
    gc.collect()

    assert saver.setup_called is True
    assert saver.closed is False, "单例仍在使用时连接上下文不应被回收"
    reset_checkpointer()
    assert contexts[0].exited is True
    assert saver.closed is True

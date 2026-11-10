"""集成测试：queue 执行后端 + worker 端到端。

要证明的是"多副本可跑"这件事真的成立，而不是"配置项存在"：
1. `TASK_EXECUTION_BACKEND=queue` 时，建任务的接口**只入队不执行**（返回时状态仍是 pending）；
2. 一个 worker 进程/线程能把它领取并跑完（真实跑完整工作流）；
3. 跑完后队列项被 ack，不会重复执行；
4. 人工审核后的恢复也走同一条队列（resume 负载带上了审核结论）；
5. worker 崩溃（租约过期）后任务能被回收重跑，不会永久卡死。
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.db.session import SessionLocal, build_session_factory
from app.models.base import utcnow
from app.models.queue import TaskQueueItem
from app.models.workflow import AnalysisTask
from app.services.task_queue import CLAIMED, DONE, PENDING, SqlTaskQueue
from app.worker import Worker
from datetime import timedelta


@pytest.fixture(autouse=True)
def clean_queue():
    """每个用例开始前清空队列：否则上一个用例"只入队不执行"留下的待办项，
    会被下一个用例的 worker 领走，断言就会指向错的任务（我第一版就踩了这个坑）。"""
    session = SessionLocal()
    try:
        session.query(TaskQueueItem).delete()
        session.commit()
    finally:
        session.close()
    yield


@pytest.fixture()
def queue_settings():
    """queue 后端 + 独立队列设置（不依赖 .env，保证可重复）。

    注意 `task_run_inline=False`：集成测试环境统一把它设成 true（同步执行便于断言），
    而它的优先级高于 `task_execution_backend`——不显式关掉的话，这里永远走 inline，
    队列测试会"看起来通过"但什么都没验证。
    """
    settings = get_settings().model_copy(
        update={
            "task_run_inline": False,
            "task_execution_backend": "queue",
            "worker_embedded": False,
            "worker_concurrency": 2,
            "worker_lease_seconds": 300,
            "worker_max_attempts": 2,
            "worker_poll_interval_seconds": 0.1,
        }
    )
    return settings


@pytest.fixture()
def queue(queue_settings) -> SqlTaskQueue:
    """队列指向测试库（集成测试的 SessionLocal 已重绑到临时库）。"""
    return SqlTaskQueue(queue_settings, session_factory=build_session_factory(SessionLocal.kw["bind"]))


def create_task_via_api(client, auth, query: str = "Android 端 3.2.0 发布后次日留存下降，原因是什么？") -> str:
    resp = client.post(
        "/api/v1/tasks",
        headers=auth["pm"],
        json={
            "query": query,
            "product": "星野社区",
            "platform": "Android",
            "app_version": "3.2.0",
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
        },
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["id"]


def test_queue_backend_enqueues_without_running(client, auth, queue, monkeypatch, queue_settings):
    """queue 后端：接口只入队，任务保持 pending（执行交给 worker）。"""
    import app.services.task_dispatch as dispatch

    monkeypatch.setattr(dispatch, "get_settings", lambda: queue_settings)
    task_id = create_task_via_api(client, auth)

    session = SessionLocal()
    try:
        task = session.get(AnalysisTask, task_id)
        assert task.status == "pending", f"queue 后端不应在接口里执行，实际状态 {task.status}"
        item = (
            session.query(TaskQueueItem)
            .filter(TaskQueueItem.task_id == task_id)
            .filter(TaskQueueItem.kind == "run")
            .one()
        )
        assert item.status in {PENDING, CLAIMED}
        assert item.attempts == 0, "还没被领取，attempts 应为 0"
    finally:
        session.close()


def test_worker_processes_enqueued_task(queue, queue_settings, client, auth, monkeypatch):
    """worker 领取并跑完入队的任务：这是"可多副本"的核心证据。"""
    import app.services.task_dispatch as dispatch

    monkeypatch.setattr(dispatch, "get_settings", lambda: queue_settings)
    task_id = create_task_via_api(client, auth)

    worker = Worker(settings=queue_settings, queue=queue, worker_id="w-test", concurrency=1)
    handled = worker.run_once()

    assert handled == 1
    assert worker.processed == 1 and worker.failed == 0

    session = SessionLocal()
    try:
        task = session.get(AnalysisTask, task_id)
        assert task.status in {"completed", "waiting_human"}, f"任务应已执行，实际 {task.status}"
        item = session.query(TaskQueueItem).filter(TaskQueueItem.task_id == task_id).one()
        assert item.status == DONE
        assert item.claimed_by == "w-test"
    finally:
        session.close()


def test_已执行完的任务不会被重复领取(queue, queue_settings, client, auth, monkeypatch):
    import app.services.task_dispatch as dispatch

    monkeypatch.setattr(dispatch, "get_settings", lambda: queue_settings)
    task_id = create_task_via_api(client, auth)
    worker = Worker(settings=queue_settings, queue=queue, worker_id="w-1", concurrency=1)
    worker.run_once()

    second = Worker(settings=queue_settings, queue=queue, worker_id="w-2", concurrency=1)
    assert second.run_once() == 0, "同一个队列项不应被第二个 worker 再跑一遍"
    assert second.processed == 0

    session = SessionLocal()
    try:
        runs = (
            session.query(TaskQueueItem)
            .filter(TaskQueueItem.task_id == task_id)
            .filter(TaskQueueItem.status == DONE)
            .all()
        )
        assert len(runs) == 1
    finally:
        session.close()


def test_worker_失败会退避重试并最终判死(queue, queue_settings):
    """坏任务不能无限重试：attempts 用尽后进 dead，不再自动领取。"""
    queue.enqueue("task-not-exists", max_attempts=2)
    worker = Worker(settings=queue_settings, queue=queue, worker_id="w-bad", concurrency=1)

    assert worker.run_once() == 1  # 第一次失败 → 回队列
    assert worker.failed == 1

    session = SessionLocal()
    try:
        item = session.query(TaskQueueItem).filter(TaskQueueItem.task_id == "task-not-exists").one()
        assert item.status == PENDING
        assert "任务不存在" in (item.last_error or "")
        item.visible_at = utcnow() - timedelta(seconds=1)  # 跳过退避等待
        session.commit()
    finally:
        session.close()

    assert worker.run_once() == 1  # 第二次失败 → 判死
    session = SessionLocal()
    try:
        item = session.query(TaskQueueItem).filter(TaskQueueItem.task_id == "task-not-exists").one()
        assert item.status == "dead"
    finally:
        session.close()

    # 判死之后不再被领取
    assert worker.run_once() == 0


def test_worker_崩溃后任务被回收重跑(queue, queue_settings, client, auth, monkeypatch):
    """模拟 worker 领了任务后进程消失：租约过期 → 回收 → 另一个 worker 跑完。"""
    import app.services.task_dispatch as dispatch

    monkeypatch.setattr(dispatch, "get_settings", lambda: queue_settings)
    task_id = create_task_via_api(client, auth)

    claimed = queue.claim("w-crashed", limit=1)
    assert len(claimed) == 1
    item_id = claimed[0].id

    session = SessionLocal()
    try:
        row = session.get(TaskQueueItem, item_id)
        assert row.status == CLAIMED
        row.lease_expires_at = utcnow() - timedelta(seconds=1)  # 假装 worker 早就挂了
        session.commit()
    finally:
        session.close()

    worker = Worker(settings=queue_settings, queue=queue, worker_id="w-healthy", concurrency=1)
    handled = worker.run_once()

    assert handled == 1, "租约过期的任务应被回收后重新执行"
    session = SessionLocal()
    try:
        assert session.get(TaskQueueItem, item_id).status == DONE
        task = session.get(AnalysisTask, task_id)
        assert task.status in {"completed", "waiting_human"}
    finally:
        session.close()


def test_审核恢复走同一条队列(queue, queue_settings, client, auth, monkeypatch):
    """resume 也要能被 worker 领取执行（否则多副本下审核结论提交后没人处理）。"""
    import app.services.task_dispatch as dispatch

    monkeypatch.setattr(dispatch, "get_settings", lambda: queue_settings)
    task_id = create_task_via_api(client, auth)
    worker = Worker(settings=queue_settings, queue=queue, worker_id="w-1", concurrency=1)
    worker.run_once()

    session = SessionLocal()
    try:
        task = session.get(AnalysisTask, task_id)
        status_after_run = task.status
    finally:
        session.close()

    if status_after_run != "waiting_human":
        pytest.skip("该用例的任务没有进入人工审核分支，跳过 resume 队列验证")

    # 直接入队一个 resume（等价于审核接口在 queue 后端下的行为）
    assert queue.enqueue(task_id, kind="resume", payload={"action": "approve", "comment": "队列验证"})

    resumed = Worker(settings=queue_settings, queue=queue, worker_id="w-2", concurrency=1)
    assert resumed.run_once() == 1

    session = SessionLocal()
    try:
        item = (
            session.query(TaskQueueItem)
            .filter(TaskQueueItem.task_id == task_id)
            .filter(TaskQueueItem.kind == "resume")
            .one()
        )
        assert item.status == DONE
        assert session.get(AnalysisTask, task_id).status == "completed"
    finally:
        session.close()

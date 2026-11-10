"""任务队列的单元测试。

队列最容易出错的地方不是"能不能取到任务"，而是这几种：
- **重复领取**：两个 worker 同时拿到同一条任务 → 同一任务被跑两遍（对模型调用是双倍花钱，
  对写库是重复数据）；
- **崩溃后卡死**：worker 挂了没人 ack，任务永远停在 claimed；
- **无限重试**：坏任务把 attempts 刷满还在队列里转；
- **重复入队**：用户重复点"重试"导致同一任务排两次。

所以下面用真实库（SQLite）去钉这几条，而不是 mock 掉。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.session import build_session_factory
from app.models.base import utcnow
from app.models.queue import TaskQueueItem
from app.services.task_queue import CLAIMED, DEAD, DONE, KIND_RESUME, PENDING, SqlTaskQueue


def make_queue(session: Session, **overrides) -> SqlTaskQueue:
    """把队列指向**测试用的临时库**：队列内部走 write_scope（全局会话工厂），
    不注入的话单元测试会直接往开发库里写数据。"""
    base = {
        "database_url": str(session.get_bind().url),
        "worker_concurrency": 2,
        "worker_lease_seconds": 60,
        "worker_max_attempts": 3,
    }
    base.update(overrides)
    settings = Settings(_env_file=None, **base)
    return SqlTaskQueue(settings, session_factory=build_session_factory(session.get_bind()))


def test_入队后可领取(session):
    queue = make_queue(session)

    item_id = queue.enqueue("task-1")
    claimed = queue.claim("w-1", limit=1)

    assert item_id
    assert len(claimed) == 1
    assert claimed[0].task_id == "task-1"
    assert claimed[0].attempts == 1, "领取时就应记一次尝试，否则崩溃会无限重试"


def test_同一任务不会被重复入队(session):
    queue = make_queue(session)

    first = queue.enqueue("task-1")
    second = queue.enqueue("task-1")

    assert first
    assert second is None, "重复入队会让同一任务被跑两遍"
    assert session.query(TaskQueueItem).count() == 1


def test_resume_与_run_可以并存(session):
    """审核恢复与重跑是两个不同动作，不应互相去重。"""
    queue = make_queue(session)

    assert queue.enqueue("task-1", kind="run")
    assert queue.enqueue("task-1", kind=KIND_RESUME)


def test_已被领取的任务不会被第二个_worker_拿到(session):
    queue = make_queue(session)
    queue.enqueue("task-1")

    first = queue.claim("w-1", limit=1)
    second = queue.claim("w-2", limit=1)

    assert len(first) == 1
    assert second == [], "同一任务被两个 worker 领到 = 重复执行"


def test_并发领取不会重复(session):
    """两个 worker 同时抢同一批任务：用线程真并发跑一遍条件更新。"""
    import threading

    queue = make_queue(session)
    for index in range(6):
        queue.enqueue(f"task-{index}")

    results: list[list[str]] = []
    lock = threading.Lock()

    def claim_as(worker: str) -> None:
        items = queue.claim(worker, limit=6)
        with lock:
            results.append([item.task_id for item in items])

    threads = [threading.Thread(target=claim_as, args=(f"w-{index}",)) for index in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    claimed = [task for group in results for task in group]
    assert len(claimed) == len(set(claimed)) == 6, f"出现重复领取: {claimed}"


def test_ack_后不再被领取(session):
    queue = make_queue(session)
    queue.enqueue("task-1")
    item = queue.claim("w-1", limit=1)[0]

    assert queue.ack(item.id, "w-1") is True
    assert queue.claim("w-2", limit=1) == []
    assert session.get(TaskQueueItem, item.id).status == DONE


def test_只有领取者能_ack(session):
    """租约过期后被别人接手时，原 worker 不能把任务标成完成。"""
    queue = make_queue(session)
    queue.enqueue("task-1")
    item = queue.claim("w-1", limit=1)[0]

    assert queue.ack(item.id, "w-2") is False
    assert session.get(TaskQueueItem, item.id).status == CLAIMED


def test_失败后退避重试(session):
    queue = make_queue(session)
    queue.enqueue("task-1")
    item = queue.claim("w-1", limit=1)[0]

    state = queue.nack(item.id, "w-1", "boom", retry_delay_seconds=30)

    assert state == PENDING
    row = session.get(TaskQueueItem, item.id)
    assert row.last_error == "boom"
    assert row.visible_at > utcnow(), "退避期内不应被再次领取"
    assert queue.claim("w-2", limit=1) == []


def test_超过最大尝试次数进_dead(session):
    queue = make_queue(session, worker_max_attempts=2)
    queue.enqueue("task-1")

    first = queue.claim("w-1", limit=1)[0]
    assert queue.nack(first.id, "w-1", "boom", retry_delay_seconds=0) == PENDING
    second = queue.claim("w-1", limit=1)[0]
    assert second.attempts == 2
    assert queue.nack(second.id, "w-1", "boom again", retry_delay_seconds=0) == DEAD

    row = session.get(TaskQueueItem, second.id)
    assert row.status == DEAD
    assert queue.claim("w-1", limit=1) == [], "dead 的任务不该再被领取"


def test_租约过期后自动回收(session):
    """worker 崩溃：没人 ack，租约到期后任务要能回到队列，而不是永远卡住。

    注意：队列用的是**独立连接**，所以夹具会话里的改动必须 commit 才能被它看到；
    不提交就只是"本地未提交的写入"，别的连接既读不到，还会把写锁一直占着。
    """
    queue = make_queue(session, worker_lease_seconds=60)
    queue.enqueue("task-1")
    item = queue.claim("w-1", limit=1)[0]

    # 手动把租约改到过去，模拟 worker 已经挂了很久
    row = session.get(TaskQueueItem, item.id)
    row.lease_expires_at = utcnow() - timedelta(seconds=5)
    session.commit()

    reclaimed = queue.reclaim_expired()

    assert reclaimed == 1
    session.expire_all()  # 队列在另一个连接改了行，本会话要丢掉缓存对象再读
    assert session.get(TaskQueueItem, item.id).status == PENDING
    again = queue.claim("w-2", limit=1)
    assert len(again) == 1 and again[0].attempts == 2, "回收后重试次数应继续累加"


def test_回收时已达上限则判死(session):
    queue = make_queue(session, worker_max_attempts=1)
    queue.enqueue("task-1")
    item = queue.claim("w-1", limit=1)[0]
    row = session.get(TaskQueueItem, item.id)
    row.lease_expires_at = utcnow() - timedelta(seconds=5)
    session.commit()

    assert queue.reclaim_expired() == 0
    session.expire_all()
    assert session.get(TaskQueueItem, item.id).status == DEAD


def test_未过期的租约不会被回收(session):
    queue = make_queue(session, worker_lease_seconds=600)
    queue.enqueue("task-1")
    queue.claim("w-1", limit=1)

    assert queue.reclaim_expired() == 0


def test_统计口径(session):
    queue = make_queue(session)
    queue.enqueue("task-1")
    queue.enqueue("task-2")
    item = queue.claim("w-1", limit=1)[0]
    queue.ack(item.id, "w-1")

    stats = queue.stats()

    assert stats["done"] == 1
    assert stats["pending"] == 1
    assert stats["claimed"] == 0
    assert stats["dead"] == 0
    assert stats["oldest_pending_at"]


def test_领取上限受_limit_约束(session):
    queue = make_queue(session)
    for index in range(5):
        queue.enqueue(f"task-{index}")

    assert len(queue.claim("w-1", limit=2)) == 2


def test_优先级高的先被领取(session):
    queue = make_queue(session)
    queue.enqueue("task-low", priority=0)
    queue.enqueue("task-high", priority=10)

    claimed = queue.claim("w-1", limit=1)

    assert claimed[0].task_id == "task-high"


def test_resume_的负载会被带给_worker(session):
    queue = make_queue(session)
    queue.enqueue("task-1", kind=KIND_RESUME, payload={"action": "approve", "comment": "ok"})

    item = queue.claim("w-1", limit=1)[0]

    assert item.kind == KIND_RESUME
    assert item.payload["action"] == "approve"


@pytest.mark.parametrize("attempts_ge_max", [True, False])
def test_已达上限的任务不再被领取(session, attempts_ge_max):
    queue = make_queue(session)
    queue.enqueue("task-1")
    row = session.query(TaskQueueItem).filter(TaskQueueItem.task_id == "task-1").one()
    row.max_attempts = 1
    row.attempts = 1 if attempts_ge_max else 0
    session.commit()  # 队列在另一个连接上读，必须提交

    claimed = queue.claim("w-1", limit=1)

    assert len(claimed) == (0 if attempts_ge_max else 1)

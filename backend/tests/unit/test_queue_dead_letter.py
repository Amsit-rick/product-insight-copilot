"""死信重放与队列运维的单元测试。

核心是**重放的语义**（不是"接口能调通"）：

1. 只有 dead 能重放 —— pending/claimed/done 一律拒绝。把已完成的项"重放"会造成
   同一任务两条队列项，那正是多副本下最容易出乱子的形态；
2. **attempts 必须清零** —— 不清零的话一被领取就又满足 attempts >= max_attempts，
   失败一次立刻再次判死，重放等于没放（这条最容易写错，也最难在界面上看出来）；
3. `last_error` 必须保留（重放之后仍要能看到"上次为什么死"），同时重放历史要留痕；
4. 重放只动队列，不动任务的其他状态。
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.session import build_session_factory
from app.models.base import utcnow
from app.models.queue import TaskQueueItem
from app.services.task_queue import DEAD, DONE, PENDING, SqlTaskQueue


def make_queue(session: Session, **overrides) -> SqlTaskQueue:
    """与 test_task_queue.py 保持同一约定：队列必须指向临时库，绝不碰开发库。"""
    base = {
        "database_url": str(session.get_bind().url),
        "worker_concurrency": 2,
        "worker_lease_seconds": 60,
        "worker_max_attempts": 3,
    }
    base.update(overrides)
    settings = Settings(_env_file=None, **base)
    return SqlTaskQueue(settings, session_factory=build_session_factory(session.get_bind()))


def add_dead(session, *, task_id: str = "task-dead", attempts: int = 3, last_error: str = "boom") -> str:
    row = TaskQueueItem(
        task_id=task_id,
        kind="run",
        payload={},
        status=DEAD,
        attempts=attempts,
        max_attempts=3,
        last_error=last_error,
        finished_at=utcnow(),
    )
    session.add(row)
    session.commit()
    return row.id


def test_只有死信可以重放(session):
    queue = make_queue(session)
    dead_id = add_dead(session)

    for item_status in (PENDING, DONE):
        row = TaskQueueItem(task_id=f"task-{item_status}", kind="run", payload={}, status=item_status, attempts=1)
        session.add(row)
        session.commit()
        assert queue.replay(row.id) == "not_dead", f"{item_status} 不该允许重放"

    assert queue.replay("does-not-exist") == "not_found"
    assert queue.replay(dead_id) == "replayed"


def test_重放会清零_attempts_否则一失败就又判死(session):
    queue = make_queue(session)
    item_id = add_dead(session, attempts=3)

    assert queue.replay(item_id) == "replayed"

    session.expire_all()
    row = session.get(TaskQueueItem, item_id)
    assert row.status == PENDING
    assert row.attempts == 0, "attempts 必须清零，否则下次失败立刻再次进入 dead"
    assert row.visible_at is not None, "应立即可见，不再有退避延迟"
    assert row.claimed_by is None and row.lease_expires_at is None and row.finished_at is None


def test_重放保留_last_error_并记录重放历史(session):
    queue = make_queue(session)
    item_id = add_dead(session, last_error="ValueError: 任务不存在")

    queue.replay(item_id)
    session.expire_all()
    row = session.get(TaskQueueItem, item_id)
    assert row.last_error == "ValueError: 任务不存在", "重放后仍要能看到上次为什么死"

    history = row.payload.get("replays") or []
    assert len(history) == 1
    assert history[0]["previous_attempts"] == 3
    assert "ValueError" in history[0]["last_error"]
    assert "at" in history[0]


def test_重放历史有上限不会无限增长(session):
    queue = make_queue(session)
    item_id = add_dead(session)
    for _ in range(8):
        assert queue.replay(item_id) == "replayed"
        # 每次重放后它又变回 pending，为了继续测"重放历史"要再置回 dead
        session.execute(text("UPDATE task_queue_items SET status = 'dead' WHERE id = :i"), {"i": item_id})
        session.commit()

    session.expire_all()
    history = session.get(TaskQueueItem, item_id).payload.get("replays") or []
    assert len(history) == 5, f"重放历史应只保留最近 5 次，实际 {len(history)} 条"


def test_重放之后能被正常领取(session):
    """重放的意义就是"放回队列能被重新领走"，这条把整条链路串起来验证。"""
    queue = make_queue(session)
    item_id = add_dead(session, attempts=3)

    assert queue.replay(item_id) == "replayed"

    claimed = queue.claim("worker-x", limit=5)
    assert [item.id for item in claimed] == [item_id]
    assert claimed[0].attempts == 1, "领取时 attempts 从 0 递增到 1，等于重新给了完整重试预算"


def test_重放全部死信(session):
    queue = make_queue(session)
    dead_ids = [add_dead(session, task_id=f"task-{index}") for index in range(3)]
    done = TaskQueueItem(task_id="task-done", kind="run", payload={}, status=DONE, attempts=1)
    session.add(done)
    session.commit()

    assert queue.replay_all() == 3
    session.expire_all()
    assert all(session.get(TaskQueueItem, item_id).status == PENDING for item_id in dead_ids)
    assert session.get(TaskQueueItem, done.id).status == DONE, "非死信不受影响"


def test_列队列项带出错误原因与重放次数(session):
    queue = make_queue(session)
    add_dead(session, task_id="task-a", last_error="RuntimeError: 炸了")
    session.add(TaskQueueItem(task_id="task-b", kind="run", payload={}, status=PENDING, attempts=0))
    session.commit()

    items = queue.list_items(status_filter=DEAD, limit=10)
    assert len(items) == 1
    view = items[0].as_dict()
    assert view["task_id"] == "task-a"
    assert view["status"] == DEAD
    assert "炸了" in (view["last_error"] or "")
    assert view["replays"] == 0

    assert len(queue.list_items(limit=10)) == 2, "不带筛选应返回全部状态"


def test_visible_at_为空的待处理项也要能被领取(session):
    """回归：`visible_at` 为 NULL 时 `NULL <= now` 在 SQL 里是 NULL（假），
    只写 `visible_at <= now` 会让这条项**永远领不走** —— 列表里一直"待处理"、
    worker 却怎么都不碰它，排查时毫无线索。

    正常路径（enqueue/nack/replay）都会写 visible_at，但列可空；
    这个测试用一条 visible_at=NULL 的记录把容错钉住。
    """
    queue = make_queue(session)
    row = TaskQueueItem(task_id="task-null-visible", kind="run", payload={}, status=PENDING, attempts=0)
    session.add(row)
    session.commit()
    assert row.visible_at is None

    claimed = queue.claim("worker-x", limit=5)
    assert [item.task_id for item in claimed] == ["task-null-visible"], "NULL 应当视为立即可见"


def test_过期租约进死信后可以重放(session):
    """真实来源之一：worker 反复崩溃导致租约回收耗尽重试次数 → 判死 → 运维重放。"""
    queue = make_queue(session)
    row = TaskQueueItem(
        task_id="task-crashed",
        kind="run",
        payload={},
        status="claimed",
        attempts=3,
        max_attempts=3,
        claimed_by="worker-dead",
        lease_expires_at=utcnow() - timedelta(seconds=5),
    )
    session.add(row)
    session.commit()
    item_id = row.id

    assert queue.reclaim_expired() == 0  # 已达上限 → 直接判死，不计入回收数
    session.expire_all()
    assert session.get(TaskQueueItem, item_id).status == DEAD

    assert queue.replay(item_id) == "replayed"
    session.expire_all()
    replayed = session.get(TaskQueueItem, item_id)
    assert replayed.status == PENDING and replayed.attempts == 0

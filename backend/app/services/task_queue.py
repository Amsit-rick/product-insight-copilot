"""任务队列（多副本安全的任务分发）。

为什么需要它：`threadpool` 后端把任务跑在 API 进程自己的线程池里，单副本没问题，
但多副本时会出两个问题：

1. **没法扩容**：任务只能在收到请求的那个副本上跑，副本再多也分摊不了；
2. **重启即丢**：进程被回收（部署/扩缩容/OOM）时，正在跑的任务直接中断。

这里用**数据库表**做队列而不是引入 Redis/Kafka：多副本部署本来就要共享 Postgres，
一张表 + 条件更新就能得到"恰好一次领取"的语义，少一个必须运维的中间件。
需要更高吞吐时，把 `TaskQueue` 接口换成 Redis Stream / SQS 即可，调用方不变。

领取的原子性靠**条件更新**而不是"先查后改":
    UPDATE task_queue_items SET status='claimed', ... WHERE id=? AND status='pending'
只有 rowcount==1 的那个 worker 算领到——SQLite 与 Postgres 都成立，不依赖 `SKIP LOCKED`。

租约（lease）：worker 崩溃后不会有人来 ack，这条任务会一直卡在 claimed。
所以领取时写 `lease_expires_at`，由 `reclaim_expired()` 把过期的放回队列（attempts 已经加过，
超过 max_attempts 的直接进 dead，避免坏任务无限重试）。
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import SessionLocal, write_scope
from app.logging_utils import get_logger
from app.models.base import utcnow
from app.models.queue import TaskQueueItem

logger = get_logger(__name__)

PENDING = "pending"
CLAIMED = "claimed"
DONE = "done"
DEAD = "dead"

KIND_RUN = "run"
KIND_RESUME = "resume"


def default_worker_id() -> str:
    """默认 worker 标识：主机名 + PID，便于在队列里看出是谁在处理。"""
    return f"{socket.gethostname()}-{__import__('os').getpid()}"


@dataclass(frozen=True)
class ClaimedItem:
    id: str
    task_id: str
    kind: str
    payload: dict
    attempts: int
    max_attempts: int
    worker_id: str


@dataclass(frozen=True)
class QueueItemView:
    """队列项的可读视图（运维用：死了哪些、为什么死的、被重放过几次）。"""

    id: str
    task_id: str
    kind: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None = None
    claimed_by: str | None = None
    finished_at: str | None = None
    created_at: str | None = None
    replays: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "last_error": self.last_error,
            "claimed_by": self.claimed_by,
            "finished_at": self.finished_at,
            "created_at": self.created_at,
            "replays": self.replays,
        }


class TaskQueue(Protocol):
    def enqueue(
        self, task_id: str, *, kind: str = KIND_RUN, payload: dict | None = None, priority: int = 0
    ) -> str | None: ...

    def claim(self, worker_id: str, *, limit: int) -> list[ClaimedItem]: ...

    def ack(self, item_id: str, worker_id: str) -> bool: ...

    def nack(self, item_id: str, worker_id: str, error: str, *, retry_delay_seconds: float = 5.0) -> str: ...

    def reclaim_expired(self) -> int: ...

    def stats(self) -> dict: ...

    def list_items(self, *, status_filter: str | None = None, limit: int = 50) -> list[QueueItemView]: ...

    def replay(self, item_id: str) -> str: ...

    def replay_all(self) -> int: ...


class SqlTaskQueue:
    """基于 `task_queue_items` 表的队列实现（SQLite / Postgres 通用）。

    `session_factory` 可注入：单元测试要指向临时库，绝不能因为队列内部用
    `write_scope()` 而把测试数据写进开发库。
    """

    def __init__(self, settings: Settings | None = None, session_factory: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._session_factory = session_factory

    def _write(self):
        return write_scope(self._session_factory)

    def _read_session(self):
        factory = self._session_factory or SessionLocal
        return factory()

    # ------------------------------------------------------------------ 入队
    def enqueue(
        self,
        task_id: str,
        *,
        kind: str = KIND_RUN,
        payload: dict | None = None,
        priority: int = 0,
        max_attempts: int | None = None,
    ) -> str | None:
        """入队。同一任务同一类型若已有未完成项，则不重复入队（返回 None）。

        幂等很重要：用户重复点"重试"、或者接口超时后客户端重发，都不该让同一任务被跑两遍。
        """
        with self._write() as session:
            existing = (
                session.query(TaskQueueItem)
                .filter(TaskQueueItem.task_id == task_id)
                .filter(TaskQueueItem.kind == kind)
                .filter(TaskQueueItem.status.in_([PENDING, CLAIMED]))
                .first()
            )
            if existing is not None:
                logger.info("任务 %s 已在队列中（%s/%s），跳过重复入队", task_id, kind, existing.status)
                return None
            item = TaskQueueItem(
                task_id=task_id,
                kind=kind,
                payload=payload or {},
                priority=priority,
                status=PENDING,
                attempts=0,
                max_attempts=max_attempts or self.settings.worker_max_attempts,
                visible_at=utcnow(),
            )
            session.add(item)
            session.flush()
            return item.id

    # ------------------------------------------------------------------ 领取
    def claim(self, worker_id: str, *, limit: int | None = None) -> list[ClaimedItem]:
        """领取至多 `limit` 条可见任务（原子、不会重复领取）。

        **读写分离**是这里的要点：先在只读会话里挑候选，再用独立的短写事务做条件更新。
        如果"挑候选 + 更新"放在同一个事务里，就是 SQLite 上最经典的**升级型事务**
        （先读后写）：一旦别的连接在中间写过，SQLite 会直接抛 `database is locked`，
        而不是等 busy_timeout——本项目在长事务问题上已经踩过一次同样的坑。
        """
        limit = limit or self.settings.worker_concurrency
        lease_seconds = self.settings.worker_lease_seconds
        now = utcnow()

        # ① 只读：挑候选（visible_at 已到、未超尝试上限，按优先级与入队时间排序）
        read = self._read_session()
        try:
            candidates = [
                row[0]
                for row in read.query(TaskQueueItem.id)
                .filter(TaskQueueItem.status == PENDING)
                # visible_at 为 NULL 要当作"立即可见"：SQL 里 `NULL <= now` 是 NULL（假），
                # 只写 `visible_at <= now` 的话，一条 visible_at 为空的队列项会**永远领不走**
                # —— 列表里一直显示"待处理"、worker 却怎么都不碰它，排查起来毫无线索。
                # 正常路径（enqueue/nack/replay）都会写 visible_at，但列本身可空，
                # 这里容错比"相信所有人都会写"更可靠。
                .filter(
                    or_(TaskQueueItem.visible_at.is_(None), TaskQueueItem.visible_at <= now)
                )
                .filter(TaskQueueItem.attempts < TaskQueueItem.max_attempts)
                .order_by(TaskQueueItem.priority.desc(), TaskQueueItem.created_at.asc())
                .limit(limit * 3)
                .all()
            ]
        finally:
            read.close()

        # ② 短写：逐条条件更新，rowcount==1 的那个 worker 才算领到
        claimed: list[ClaimedItem] = []
        for item_id in candidates:
            if len(claimed) >= limit:
                break
            with self._write() as session:
                updated = (
                    session.query(TaskQueueItem)
                    .filter(TaskQueueItem.id == item_id)
                    .filter(TaskQueueItem.status == PENDING)
                    .update(
                        {
                            TaskQueueItem.status: CLAIMED,
                            TaskQueueItem.claimed_by: worker_id,
                            TaskQueueItem.claimed_at: now,
                            TaskQueueItem.lease_expires_at: now + timedelta(seconds=lease_seconds),
                            TaskQueueItem.attempts: TaskQueueItem.attempts + 1,
                            TaskQueueItem.visible_at: now + timedelta(seconds=lease_seconds),
                        },
                        synchronize_session=False,
                    )
                )
                if updated != 1:
                    continue  # 被别的 worker 抢先领走了
                row = session.get(TaskQueueItem, item_id)
                claimed.append(
                    ClaimedItem(
                        id=row.id,
                        task_id=row.task_id,
                        kind=row.kind,
                        payload=dict(row.payload or {}),
                        attempts=row.attempts,
                        max_attempts=row.max_attempts,
                        worker_id=worker_id,
                    )
                )
        return claimed

    # ------------------------------------------------------------------ 完成/失败
    def ack(self, item_id: str, worker_id: str) -> bool:
        """标记完成。只有领取者本人能 ack（防止租约过期后被别人接手时误标）。"""
        with self._write() as session:
            updated = (
                session.query(TaskQueueItem)
                .filter(TaskQueueItem.id == item_id)
                .filter(TaskQueueItem.status == CLAIMED)
                .filter(TaskQueueItem.claimed_by == worker_id)
                .update(
                    {
                        TaskQueueItem.status: DONE,
                        TaskQueueItem.finished_at: utcnow(),
                        TaskQueueItem.lease_expires_at: None,
                    },
                    synchronize_session=False,
                )
            )
            return updated == 1

    def nack(
        self,
        item_id: str,
        worker_id: str,
        error: str,
        *,
        retry_delay_seconds: float = 5.0,
    ) -> str:
        """执行失败：未超过上限则退避重试，超过则进 dead（不再自动重试）。"""
        with self._write() as session:
            row = session.get(TaskQueueItem, item_id)
            if row is None or row.status != CLAIMED or row.claimed_by != worker_id:
                return "ignored"
            row.last_error = str(error)[:1000]
            row.claimed_by = None
            row.claimed_at = None
            row.lease_expires_at = None
            if row.attempts >= row.max_attempts:
                row.status = DEAD
                row.finished_at = utcnow()
                logger.error(
                    "任务 %s 已尝试 %s 次仍失败，标记 dead：%s", row.task_id, row.attempts, error
                )
                return DEAD
            row.status = PENDING
            row.visible_at = utcnow() + timedelta(seconds=max(0.0, retry_delay_seconds))
            logger.warning(
                "任务 %s 第 %s 次执行失败，%.0f 秒后重试：%s",
                row.task_id,
                row.attempts,
                retry_delay_seconds,
                error,
            )
            return PENDING

    def reclaim_expired(self) -> int:
        """把租约过期的任务放回队列（worker 崩溃后的自愈）。

        attempts 在领取时就加过了，所以这里不重复加：超过上限的直接进 dead，
        否则"每次崩溃都算一次重试"会把 max_attempts 迅速耗尽。
        """
        now = utcnow()
        with self._write() as session:
            rows = (
                session.query(TaskQueueItem)
                .filter(TaskQueueItem.status == CLAIMED)
                .filter(TaskQueueItem.lease_expires_at.isnot(None))
                .filter(TaskQueueItem.lease_expires_at < now)
                .all()
            )
            reclaimed = 0
            for row in rows:
                row.claimed_by = None
                row.claimed_at = None
                row.lease_expires_at = None
                if row.attempts >= row.max_attempts:
                    row.status = DEAD
                    row.finished_at = now
                    row.last_error = (row.last_error or "") + "；租约过期且已达最大尝试次数"
                else:
                    row.status = PENDING
                    row.visible_at = now
                    row.last_error = (row.last_error or "") + "；worker 未在租约内确认，已回收"
                    reclaimed += 1
            if rows:
                logger.warning("回收了 %s 条租约过期的任务（另有 %s 条进入 dead）", reclaimed, len(rows) - reclaimed)
            return reclaimed

    # ------------------------------------------------------------------ 观测
    def stats(self) -> dict[str, Any]:
        """队列概览（供 /health 与运维看板）。只读查询，用普通会话即可。"""
        session = self._read_session()
        try:
            rows = (
                session.query(TaskQueueItem.status, func.count(TaskQueueItem.id))
                .group_by(TaskQueueItem.status)
                .all()
            )
            by_status = {status: int(count) for status, count in rows}
            oldest = (
                session.query(func.min(TaskQueueItem.created_at))
                .filter(TaskQueueItem.status == PENDING)
                .scalar()
            )
            return {
                "pending": by_status.get(PENDING, 0),
                "claimed": by_status.get(CLAIMED, 0),
                "done": by_status.get(DONE, 0),
                "dead": by_status.get(DEAD, 0),
                "oldest_pending_at": oldest.isoformat() if isinstance(oldest, datetime) else None,
            }
        finally:
            session.close()

    # ------------------------------------------------------------- 死信运维
    def list_items(self, *, status_filter: str | None = None, limit: int = 50) -> list[QueueItemView]:
        """列队列项（默认按创建时间倒序），供界面核对"死了哪些、为什么死的"。

        在此之前只有 `stats()` 给出的**计数**（dead: N）—— 看得到数量、看不到是哪些任务、
        也看不到 `last_error`，运维只能干瞪眼。这是补齐这个缺口的第一个入口。
        """
        session = self._read_session()
        try:
            query = session.query(TaskQueueItem)
            if status_filter:
                query = query.filter(TaskQueueItem.status == status_filter)
            rows = query.order_by(TaskQueueItem.created_at.desc()).limit(max(1, int(limit))).all()
            return [
                QueueItemView(
                    id=row.id,
                    task_id=row.task_id,
                    kind=row.kind,
                    status=row.status,
                    attempts=int(row.attempts or 0),
                    max_attempts=int(row.max_attempts or 0),
                    last_error=row.last_error,
                    claimed_by=row.claimed_by,
                    finished_at=row.finished_at.isoformat() if row.finished_at else None,
                    created_at=row.created_at.isoformat() if row.created_at else None,
                    replays=len((row.payload or {}).get("replays") or []),
                )
                for row in rows
            ]
        finally:
            session.close()

    def replay(self, item_id: str) -> str:
        """把一条**死信**重新放回队列；返回 `replayed` / `not_found` / `not_dead`。

        与 `POST /tasks/{id}/retry` 的区别（两者都该存在，语义不同）：
        - task retry 面向**任务**：新建一条队列项，任务侧 `retry_count` 累加；
        - 死信重放面向**队列项**：复用这条死信本身，保留"为什么死的"（`last_error`）与重放历史。

        关键的一条：`attempts` 必须清零。不清零的话，这条项一被领取就又满足
        `attempts >= max_attempts`，失败一次立刻再次判死 —— 重放等于没放。
        """
        with self._write() as session:
            row = session.get(TaskQueueItem, item_id)
            if row is None:
                return "not_found"
            if row.status != DEAD:
                return "not_dead"
            payload = dict(row.payload or {})
            history = list(payload.get("replays") or [])
            history.append(
                {
                    "at": utcnow().isoformat(),
                    "previous_attempts": int(row.attempts or 0),
                    # 只留错误的开头，避免 payload 被长堆栈撑爆
                    "last_error": (row.last_error or "")[:200],
                }
            )
            payload["replays"] = history[-5:]  # 保留最近 5 次，防止无限增长
            row.payload = payload
            row.status = PENDING
            row.attempts = 0
            row.visible_at = utcnow()
            row.claimed_by = None
            row.claimed_at = None
            row.lease_expires_at = None
            row.finished_at = None
            # last_error 故意**保留**：重放之后仍然要能看到上次为什么死
            logger.warning("死信重放：item=%s task=%s（第 %s 次重放）", item_id, row.task_id, len(payload["replays"]))
            return "replayed"

    def replay_all(self) -> int:
        """把当前所有死信重新放回队列，返回重放条数。"""
        session = self._read_session()
        try:
            ids = [
                row[0]
                for row in session.query(TaskQueueItem.id).filter(TaskQueueItem.status == DEAD).all()
            ]
        finally:
            session.close()
        replayed = 0
        for item_id in ids:
            if self.replay(item_id) == "replayed":
                replayed += 1
        return replayed


def get_task_queue(settings: Settings | None = None) -> SqlTaskQueue:
    return SqlTaskQueue(settings)


__all__ = [
    "CLAIMED",
    "DEAD",
    "DONE",
    "KIND_RESUME",
    "KIND_RUN",
    "PENDING",
    "ClaimedItem",
    "QueueItemView",
    "SqlTaskQueue",
    "TaskQueue",
    "default_worker_id",
    "get_task_queue",
]

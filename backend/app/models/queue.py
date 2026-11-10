"""任务队列表（多副本安全的任务分发，见 `services/task_queue.py`）。

一张表解决三件事：任务分发、崩溃自愈（租约）、失败重试（attempts/visible_at）。
字段设计都围绕这三件事，不做"通用队列"的抽象。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPkMixin, TimestampMixin


class TaskQueueItem(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "task_queue_items"
    __table_args__ = (
        # 领取时按 (status, visible_at) 过滤，这个索引是热路径
        Index("ix_queue_status_visible", "status", "visible_at"),
        Index("ix_queue_task_kind", "task_id", "kind"),
    )

    task_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # run=执行任务；resume=人工审核后恢复
    kind: Mapped[str] = mapped_column(String(16), default="run")
    # resume 需要携带审核结论，因此队列项要能存一点上下文（JSON，与其它表一致）
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    # 在此时间点之前不可被领取（用于失败退避与租约占位）
    visible_at: Mapped[datetime | None] = mapped_column(DateTime)
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

"""Persistent leases for singleton background schedulers."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base, TimestampMixin


class SchedulerLease(TimestampMixin, Base):
    __tablename__ = "scheduler_leases"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(String(128), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_run_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_run_finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)


__all__ = ["SchedulerLease"]

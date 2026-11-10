"""SQLAlchemy 声明基类与公共混入。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    repr_cols_num = 4

    def as_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class UUIDPkMixin:
    """字符串 UUID 主键，便于跨库（Postgres / SQLite）保持一致。"""

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id, sort_order=-100
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now(), nullable=False, sort_order=100
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
        sort_order=101,
    )

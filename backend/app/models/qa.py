"""知识库问答会话与消息：用于归档、恢复和连续追问。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base, TimestampMixin, UUIDPkMixin, utcnow


class QaConversation(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "qa_conversations"
    __table_args__ = (
        Index("ix_qa_conversation_owner_updated", "tenant_id", "user_id", "last_message_at"),
    )

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        nullable=False,
        index=True,
    )


class QaMessage(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "qa_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_qa_message_conversation_sequence"),
        Index("ix_qa_message_conversation_created", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    request_options: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    source_audit_id: Mapped[str | None] = mapped_column(String(36), unique=True, index=True)


__all__ = ["QaConversation", "QaMessage"]

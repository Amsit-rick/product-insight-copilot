"""知识库、文档、版本与切片。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base, TimestampMixin, UUIDPkMixin
from app.models.enums import DocumentStatus, KnowledgeBaseCode, SecurityLevel


class KnowledgeBase(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_kb_tenant_code"),)

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    code: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(64))
    security_level: Mapped[str] = mapped_column(
        String(32), default=SecurityLevel.INTERNAL.value, index=True
    )
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    index_status: Mapped[str] = mapped_column(String(32), default="empty")
    embedding_version: Mapped[str | None] = mapped_column(String(32))
    index_version: Mapped[str | None] = mapped_column(String(32))
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime)

    def as_api(self) -> dict:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "security_level": self.security_level,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "index_status": self.index_status,
        }


class Document(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "documents"

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    kb_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    source_name: Mapped[str] = mapped_column(String(255))
    source_type: Mapped[str] = mapped_column(String(32), default="upload")
    storage_key: Mapped[str | None] = mapped_column(String(512))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    department: Mapped[str | None] = mapped_column(String(64))
    security_level: Mapped[str] = mapped_column(
        String(32), default=SecurityLevel.INTERNAL.value, index=True
    )
    document_version: Mapped[str] = mapped_column(String(32), default="v1")
    status: Mapped[str] = mapped_column(
        String(32), default=DocumentStatus.UPLOADED.value, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    doc_updated_at: Mapped[date | None] = mapped_column(Date)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    uploaded_by: Mapped[str | None] = mapped_column(String(36))

    def kb_code(self) -> str:
        return (self.meta or {}).get("kb_code", "")


class DocumentVersion(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "document_versions"

    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), index=True)
    version: Mapped[str] = mapped_column(String(32))
    storage_key: Mapped[str | None] = mapped_column(String(512))
    checksum: Mapped[str | None] = mapped_column(String(128))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    change_note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36))


class DocumentChunk(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        Index("ix_chunk_kb_tenant", "tenant_id", "kb_code"),
        Index("ix_chunk_doc", "document_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    document_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    kb_code: Mapped[str] = mapped_column(String(64), index=True)
    document_version: Mapped[str] = mapped_column(String(32), default="v1")
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    page: Mapped[int | None] = mapped_column(Integer)
    section_title: Mapped[str | None] = mapped_column(String(255))
    chunk_type: Mapped[str] = mapped_column(String(32), default="semantic")
    source_name: Mapped[str | None] = mapped_column(String(255))
    department: Mapped[str | None] = mapped_column(String(64))
    security_level: Mapped[str] = mapped_column(
        String(32), default=SecurityLevel.INTERNAL.value, index=True
    )
    app_version: Mapped[str | None] = mapped_column(String(32), index=True)
    doc_updated_at: Mapped[date | None] = mapped_column(Date)
    # JSON 形式保存向量，供 fallback 向量库使用；pgvector 由 chunk_vectors 表承载
    embedding: Mapped[list | None] = mapped_column(JSON)
    embedding_version: Mapped[str | None] = mapped_column(String(32))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


def kb_code_of(kb: KnowledgeBase | None) -> str:
    return kb.code if kb else KnowledgeBaseCode.PRODUCT.value

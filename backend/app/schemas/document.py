"""知识库与文档契约。"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeBaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    name: str
    description: str | None = None
    security_level: str
    document_count: int = 0
    chunk_count: int = 0
    index_status: str = "empty"
    last_indexed_at: datetime | None = None


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kb_id: str
    kb_code: str | None = None
    name: str
    source_name: str
    source_type: str
    mime_type: str | None = None
    size_bytes: int = 0
    department: str | None = None
    security_level: str
    document_version: str
    status: str
    error_message: str | None = None
    doc_updated_at: date | None = None
    chunk_count: int = 0
    uploaded_by: str | None = None
    created_at: datetime


class DocumentUploadResponse(BaseModel):
    document: DocumentOut
    chunks: int = 0
    warnings: list[str] = Field(default_factory=list)
    index_status: str = "indexed"


class ReindexRequest(BaseModel):
    kb_code: str | None = Field(default=None, description="留空则重建全部可见知识库")
    force: bool = Field(default=False, description="强制重算 Embedding，忽略版本一致")


class ReindexResponse(BaseModel):
    kb_code: str
    documents: int = 0
    chunks: int = 0
    failed: list[dict] = Field(default_factory=list)
    index_version: str | None = None
    embedding_version: str | None = None
    duration_ms: int = 0


class DocumentDeleteResponse(BaseModel):
    document_id: str
    deleted_chunks: int = 0
    index_status: str = "deleted"

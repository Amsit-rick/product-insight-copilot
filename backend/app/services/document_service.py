"""文档入库服务（设计文档 9.2 节）。

    上传 -> 安全检查 -> 存储 -> 解析 -> 清洗与切片 -> 写入元数据 -> Embedding -> 向量写入

要点：
- 安全检查不通过直接拒绝，并记录审计；
- 每个切片都带齐租户、知识库、部门、版本、时间、密级元数据（检索时强制过滤）；
- Embedding 与向量写入通过 `VectorStore` 接口完成，SQLite 环境自动走 Python 余弦回退；
- 解析失败不会让文档消失：状态置 `failed` 并保存错误原因，便于重试。
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_utils import get_logger
from app.models.base import utcnow
from app.models.enums import DocumentStatus, KnowledgeBaseCode, SecurityLevel
from app.models.identity import User
from app.models.knowledge import Document, DocumentChunk, DocumentVersion, KnowledgeBase
from app.rag.chunking import build_chunks
from app.rag.parsers import ParseError, parse_file
from app.rag.vectorstore import get_vector_store
from app.services.audit import record_audit
from app.services.model_gateway import get_embedding_client
from app.services.security import UploadSecurityError, check_upload
from app.services.storage import save_upload

logger = get_logger(__name__)

EMBED_BATCH = 32


class DocumentService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    # ------------------------------------------------------------------ 知识库
    def ensure_kb(
        self,
        tenant_id: str,
        code: str,
        *,
        name: str | None = None,
        description: str | None = None,
        security_level: str = SecurityLevel.INTERNAL.value,
    ) -> KnowledgeBase:
        kb = (
            self.session.query(KnowledgeBase)
            .filter(KnowledgeBase.tenant_id == tenant_id)
            .filter(KnowledgeBase.code == code)
            .first()
        )
        if kb is not None:
            return kb
        kb = KnowledgeBase(
            tenant_id=tenant_id,
            code=code,
            name=name or _default_kb_name(code),
            description=description,
            security_level=security_level,
            index_status="empty",
            embedding_version=self.settings.embedding_version,
            index_version=self.settings.index_version,
        )
        self.session.add(kb)
        self.session.flush()
        return kb

    def list_kbs(self, tenant_id: str, *, allowed_codes: set[str] | None = None) -> list[KnowledgeBase]:
        query = self.session.query(KnowledgeBase).filter(KnowledgeBase.tenant_id == tenant_id)
        if allowed_codes is not None:
            query = query.filter(KnowledgeBase.code.in_(sorted(allowed_codes) or [""]))
        return query.order_by(KnowledgeBase.code).all()

    # ------------------------------------------------------------------ 入库
    def ingest(
        self,
        *,
        tenant_id: str,
        user: User | None,
        kb_code: str,
        filename: str,
        data: bytes,
        name: str | None = None,
        source_name: str | None = None,
        security_level: str = SecurityLevel.INTERNAL.value,
        document_version: str = "v1",
        department: str | None = None,
        app_version: str | None = None,
        doc_updated_at: date | None = None,
        source_type: str = "upload",
        meta: dict | None = None,
    ) -> dict:
        check = check_upload(filename, data)
        if not check.ok:
            record_audit(
                self.session,
                action="document_upload_rejected",
                tenant_id=tenant_id,
                actor_id=user.id if user else None,
                resource_type="document",
                resource_id=filename,
                outcome="deny",
                detail={"errors": check.errors},
            )
            self.session.flush()
            raise UploadSecurityError(check.errors)

        kb = self.ensure_kb(tenant_id, kb_code)
        storage_key = save_upload(filename, data, tenant_id)
        suffix = Path(filename).suffix.lower()

        document = Document(
            tenant_id=tenant_id,
            kb_id=kb.id,
            name=name or Path(filename).stem,
            source_name=source_name or filename,
            source_type=source_type,
            storage_key=storage_key,
            mime_type=_mime_of(suffix),
            size_bytes=len(data),
            department=department,
            security_level=security_level,
            document_version=document_version,
            status=DocumentStatus.PARSING.value,
            doc_updated_at=doc_updated_at or date.today(),
            meta={
                **(meta or {}),
                "kb_code": kb_code,
                # 版本号用于检索时按版本过滤（documents 表本身不存版本维度）
                **({"app_version": app_version} if app_version else {}),
            },
            uploaded_by=user.id if user else None,
        )
        self.session.add(document)
        self.session.flush()

        self.session.add(
            DocumentVersion(
                tenant_id=tenant_id,
                document_id=document.id,
                version=document_version,
                storage_key=storage_key,
                chunk_count=0,
                change_note="初次入库",
                created_by=user.id if user else None,
            )
        )
        self.session.flush()

        warnings = list(check.warnings)
        try:
            blocks = self._parse_uploaded(data, suffix)
            drafts = build_chunks(blocks)
        except ParseError as exc:
            document.status = DocumentStatus.FAILED.value
            document.error_message = str(exc)[:1000]
            self.session.flush()
            record_audit(
                self.session,
                action="document_parse_failed",
                tenant_id=tenant_id,
                actor_id=user.id if user else None,
                resource_type="document",
                resource_id=document.id,
                outcome="failed",
                detail={"error": str(exc)},
            )
            self.session.flush()
            logger.warning("文档解析失败 document_id=%s: %s", document.id, exc)
            return {
                "document": document,
                "chunks": 0,
                "warnings": [*warnings, f"解析失败：{exc}"],
                "index_status": DocumentStatus.FAILED.value,
            }

        chunks = self._store_chunks(document, kb, drafts, doc_updated_at=document.doc_updated_at)
        self._embed_chunks(chunks)
        document.status = DocumentStatus.INDEXED.value
        document.error_message = None
        self._refresh_kb_stats(kb)

        record_audit(
            self.session,
            action="document_ingested",
            tenant_id=tenant_id,
            actor_id=user.id if user else None,
            resource_type="document",
            resource_id=document.id,
            detail={
                "kb_code": kb_code,
                "chunks": len(chunks),
                "security_level": security_level,
                "document_version": document_version,
                "warnings": warnings,
            },
        )
        self.session.flush()
        logger.info(
            "文档入库完成 document_id=%s kb=%s chunks=%s", document.id, kb_code, len(chunks)
        )
        return {
            "document": document,
            "chunks": len(chunks),
            "warnings": warnings,
            "index_status": DocumentStatus.INDEXED.value,
        }

    # ------------------------------------------------------------------ 重建索引
    def reindex(
        self,
        *,
        tenant_id: str,
        kb_code: str,
        user: User | None = None,
        force: bool = False,
    ) -> dict:
        kb = (
            self.session.query(KnowledgeBase)
            .filter(KnowledgeBase.tenant_id == tenant_id)
            .filter(KnowledgeBase.code == kb_code)
            .first()
        )
        if kb is None:
            raise ValueError(f"知识库不存在: {kb_code}")

        documents = (
            self.session.query(Document)
            .filter(Document.kb_id == kb.id)
            .filter(Document.status != DocumentStatus.DELETED.value)
            .all()
        )
        failed: list[dict] = []
        chunk_total = 0
        for document in documents:
            try:
                data = None
                if document.storage_key:
                    from app.services.storage import get_storage

                    data = get_storage().get(document.storage_key)
                if data is None:
                    raise ParseError("原始文件缺失，无法重建索引")
                blocks = self._parse_uploaded(data, Path(document.source_name).suffix.lower())
                drafts = build_chunks(blocks)
                store = get_vector_store(self.session)
                store.delete_document(self.session, document.id)
                self.session.query(DocumentChunk).filter(
                    DocumentChunk.document_id == document.id
                ).delete(synchronize_session=False)
                self.session.flush()
                chunks = self._store_chunks(
                    document,
                    kb,
                    drafts,
                    doc_updated_at=document.doc_updated_at,
                    force=force,
                )
                self._embed_chunks(chunks)
                document.status = DocumentStatus.INDEXED.value
                document.error_message = None
                chunk_total += len(chunks)
            except Exception as exc:
                document.status = DocumentStatus.FAILED.value
                document.error_message = str(exc)[:1000]
                failed.append({"document_id": document.id, "error": str(exc)})
                logger.warning("重建索引失败 document_id=%s: %s", document.id, exc)
        kb.index_version = self.settings.index_version
        kb.embedding_version = self.settings.embedding_version
        kb.last_indexed_at = utcnow()
        self._refresh_kb_stats(kb)
        record_audit(
            self.session,
            action="kb_reindexed",
            tenant_id=tenant_id,
            actor_id=user.id if user else None,
            resource_type="knowledge_base",
            resource_id=kb.id,
            outcome="partial" if failed else "success",
            detail={"documents": len(documents), "chunks": chunk_total, "failed": failed},
        )
        self.session.flush()
        return {
            "kb_code": kb_code,
            "documents": len(documents),
            "chunks": chunk_total,
            "failed": failed,
            "index_version": kb.index_version,
            "embedding_version": kb.embedding_version,
        }

    # ------------------------------------------------------------------ 删除
    def delete(self, document: Document, *, user: User | None = None) -> int:
        chunk_count = (
            self.session.query(DocumentChunk)
            .filter(DocumentChunk.document_id == document.id)
            .delete(synchronize_session=False)
        )
        get_vector_store(self.session).delete_document(self.session, document.id)
        document.status = DocumentStatus.DELETED.value
        kb = self.session.get(KnowledgeBase, document.kb_id)
        if kb is not None:
            self._refresh_kb_stats(kb)
        record_audit(
            self.session,
            action="document_deleted",
            tenant_id=document.tenant_id,
            actor_id=user.id if user else None,
            resource_type="document",
            resource_id=document.id,
            detail={"chunks": int(chunk_count)},
        )
        self.session.flush()
        return int(chunk_count)

    # ------------------------------------------------------------------ 内部
    def _parse_uploaded(self, data: bytes, suffix: str):
        tmp_dir = Path(self.settings.storage_local_dir) / "_parse_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path = tmp_dir / f"{uuid.uuid4().hex}{suffix}"
        try:
            path.write_bytes(data)
            return parse_file(path, suffix)
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:  # pragma: no cover
                pass

    def _store_chunks(
        self,
        document: Document,
        kb: KnowledgeBase,
        drafts,
        *,
        doc_updated_at: date | None,
        force: bool = False,
    ) -> list[DocumentChunk]:
        chunks: list[DocumentChunk] = []
        for draft in drafts:
            chunk = DocumentChunk(
                tenant_id=document.tenant_id,
                document_id=document.id,
                kb_id=kb.id,
                kb_code=kb.code,
                document_version=document.document_version,
                chunk_index=draft.chunk_index,
                content=draft.content,
                token_count=draft.token_count,
                page=draft.page,
                section_title=draft.section_title,
                chunk_type=draft.block_type,
                source_name=document.source_name,
                department=document.department,
                security_level=document.security_level,
                app_version=(document.meta or {}).get("app_version"),
                doc_updated_at=doc_updated_at,
                embedding_version=self.settings.embedding_version,
                meta={"document_name": document.name, "kb_code": kb.code},
            )
            self.session.add(chunk)
            chunks.append(chunk)
        self.session.flush()
        return chunks

    def _embed_chunks(self, chunks: list[DocumentChunk]) -> None:
        if not chunks:
            return
        embedder = get_embedding_client()
        store = get_vector_store(self.session)
        for start in range(0, len(chunks), EMBED_BATCH):
            batch = chunks[start : start + EMBED_BATCH]
            vectors = embedder.embed([c.content for c in batch])
            ids = [c.id for c in batch]
            # 向量写在哪里**完全由 store 决定**：
            # - FallbackVectorStore → 写进 document_chunks.embedding（关系库里留向量）
            # - pgvector / qdrant   → 写进各自的向量库（关系库里**不留**向量）
            # 这条契约很重要：换成外部向量后端后关系库里没有向量，
            # 把后端切回 fallback 会检索不到任何东西 —— 必须先重建索引
            # （scripts/reindex_vectors.py 或 reindex 接口）。原来这里写的是
            # `if store.name != "pgvector": pass`（一个什么都不做的分支），
            # 新增 qdrant 之后语义更含糊，所以改成显式记录实际行为。
            store.upsert(self.session, ids, vectors)
            if not store.persists_in_database:
                logger.debug(
                    "向量写入外部向量库 %s（关系库不留向量，本批 %s 条）", store.name, len(ids)
                )
        self.session.flush()

    def _refresh_kb_stats(self, kb: KnowledgeBase) -> None:
        from sqlalchemy import func

        # 必须先 flush：会话是 autoflush=False，调用方往往刚刚改过 Document 状态
        # （删除时把 status 置为 deleted）或新增了文档行却还没落库。
        # 不 flush 就统计，会把已删除的文档继续算进 document_count —— 删完之后
        # 知识库概览的数字不变，这个 bug 真出现过（2026-09-15 清理误传文档时发现）。
        self.session.flush()
        doc_count = (
            self.session.query(func.count(Document.id))
            .filter(Document.kb_id == kb.id)
            .filter(Document.status != DocumentStatus.DELETED.value)
            .scalar()
        )
        chunk_count = (
            self.session.query(func.count(DocumentChunk.id))
            .filter(DocumentChunk.kb_id == kb.id)
            .scalar()
        )
        kb.document_count = int(doc_count or 0)
        kb.chunk_count = int(chunk_count or 0)
        kb.index_status = "indexed" if chunk_count else "empty"
        kb.last_indexed_at = kb.last_indexed_at or utcnow()
        self.session.flush()


def _default_kb_name(code: str) -> str:
    return {
        KnowledgeBaseCode.PRODUCT.value: "产品知识库",
        KnowledgeBaseCode.FEEDBACK.value: "用户反馈知识库",
        KnowledgeBaseCode.ENGINEERING.value: "研发知识库",
        KnowledgeBaseCode.OPERATION.value: "运营知识库",
    }.get(code, code)


def _mime_of(suffix: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".csv": "text/csv",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".json": "application/json",
    }.get(suffix, "application/octet-stream")

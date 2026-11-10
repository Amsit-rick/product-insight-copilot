"""知识库与文档接口（上传、查看、删除、重建索引）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import Pagination, Principal, get_current_principal, get_pagination
from app.db.session import get_db
from app.models.enums import DocumentStatus, SecurityLevel
from app.models.knowledge import Document
from app.schemas.common import ListResponse, PageMeta
from app.schemas.document import (
    DocumentDeleteResponse,
    DocumentOut,
    DocumentUploadResponse,
    KnowledgeBaseOut,
    ReindexRequest,
    ReindexResponse,
)
from app.services.document_service import DocumentService
from app.services.security import UploadSecurityError

router = APIRouter(tags=["知识库与文档"])


def _document_out(session: Session, document: Document) -> DocumentOut:
    from sqlalchemy import func

    from app.models.knowledge import DocumentChunk

    chunk_count = (
        session.query(func.count(DocumentChunk.id))
        .filter(DocumentChunk.document_id == document.id)
        .scalar()
        or 0
    )
    return DocumentOut(
        id=document.id,
        kb_id=document.kb_id,
        kb_code=(document.meta or {}).get("kb_code"),
        name=document.name,
        source_name=document.source_name,
        source_type=document.source_type,
        mime_type=document.mime_type,
        size_bytes=document.size_bytes,
        department=document.department,
        security_level=document.security_level,
        document_version=document.document_version,
        status=document.status,
        error_message=document.error_message,
        doc_updated_at=document.doc_updated_at,
        chunk_count=int(chunk_count),
        uploaded_by=document.uploaded_by,
        created_at=document.created_at,
    )


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseOut], summary="可见知识库列表")
def list_knowledge_bases(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> list[KnowledgeBaseOut]:
    service = DocumentService(session)
    items = service.list_kbs(principal.tenant_id, allowed_codes=principal.knowledge_bases)
    return [KnowledgeBaseOut.model_validate(item) for item in items]


@router.post(
    "/documents",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="上传文档并入库",
)
def upload_document(
    file: UploadFile = File(...),
    kb_code: str = Form(...),
    name: str | None = Form(default=None),
    document_version: str = Form(default="v1"),
    security_level: str = Form(default=SecurityLevel.INTERNAL.value),
    department: str | None = Form(default=None),
    app_version: str | None = Form(default=None),
    doc_updated_at: str | None = Form(default=None),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> DocumentUploadResponse:
    principal.require("document:upload")
    if kb_code not in principal.knowledge_bases:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "permission_denied", "message": f"无权写入知识库: {kb_code}"},
        )
    data = file.file.read()
    service = DocumentService(session)
    try:
        result = service.ingest(
            tenant_id=principal.tenant_id,
            user=principal.user,
            kb_code=kb_code,
            filename=file.filename or "upload.bin",
            data=data,
            name=name,
            document_version=document_version,
            security_level=security_level,
            department=department,
            app_version=app_version,
            doc_updated_at=_parse_date(doc_updated_at),
        )
    except UploadSecurityError as exc:
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "upload_rejected", "message": str(exc), "errors": exc.errors},
        ) from exc
    session.commit()
    document = result["document"]
    return DocumentUploadResponse(
        document=_document_out(session, document),
        chunks=result["chunks"],
        warnings=result["warnings"],
        index_status=result["index_status"],
    )


@router.get("/documents", response_model=ListResponse[DocumentOut], summary="文档列表")
def list_documents(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    pagination: Pagination = Depends(get_pagination),
    kb_code: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
) -> ListResponse[DocumentOut]:
    query = session.query(Document).filter(Document.tenant_id == principal.tenant_id)
    visible_codes = sorted(principal.knowledge_bases)
    if kb_code:
        if kb_code not in principal.knowledge_bases:
            raise HTTPException(status_code=403, detail="无权访问该知识库")
        visible_codes = [kb_code]
    if status_filter:
        query = query.filter(Document.status == status_filter)
    else:
        # 默认**不返回**已软删除的文档：删除之后列表里就应该看不到它了
        # （原来是"不传 status 就不过滤"，于是删掉的文档一直挂在列表和总数里，
        #   与手册"删除后列表消失"的预期不符 —— 真机核对时才发现）。
        # 想看已删除的显式传 status=deleted，界面「全部状态」下拉里就有这一项。
        query = query.filter(Document.status != DocumentStatus.DELETED.value)
    documents = query.order_by(Document.created_at.desc()).all()
    documents = [
        doc for doc in documents if (doc.meta or {}).get("kb_code") in set(visible_codes)
    ]
    total = len(documents)
    page_items = documents[pagination.offset : pagination.offset + pagination.page_size]
    return ListResponse[DocumentOut](
        items=[_document_out(session, doc) for doc in page_items],
        meta=PageMeta(total=total, page=pagination.page, page_size=pagination.page_size),
    )


@router.get("/documents/{document_id}", response_model=DocumentOut, summary="文档详情")
def get_document(
    document_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> DocumentOut:
    document = session.get(Document, document_id)
    if document is None or document.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb_code = (document.meta or {}).get("kb_code")
    if kb_code and kb_code not in principal.knowledge_bases:
        raise HTTPException(status_code=403, detail="无权访问该文档")
    return _document_out(session, document)


@router.delete(
    "/documents/{document_id}",
    response_model=DocumentDeleteResponse,
    summary="删除文档（同时清理切片与向量）",
)
def delete_document(
    document_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> DocumentDeleteResponse:
    principal.require("document:delete")
    document = session.get(Document, document_id)
    if document is None or document.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    chunks = DocumentService(session).delete(document, user=principal.user)
    session.commit()
    return DocumentDeleteResponse(document_id=document_id, deleted_chunks=chunks)


@router.post(
    "/knowledge-bases/{kb_code}/reindex",
    response_model=ReindexResponse,
    summary="重建知识库索引",
)
def reindex_knowledge_base(
    kb_code: str,
    payload: ReindexRequest | None = None,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> ReindexResponse:
    principal.require("kb:reindex")
    if kb_code not in principal.knowledge_bases:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "permission_denied", "message": f"无权重建知识库: {kb_code}"},
        )
    service = DocumentService(session)
    result = service.reindex(
        tenant_id=principal.tenant_id,
        kb_code=kb_code,
        user=principal.user,
        force=bool(payload.force) if payload else False,
    )
    session.commit()
    return ReindexResponse(**result)


def _parse_date(value: str | None):
    if not value:
        return None
    from datetime import date

    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None

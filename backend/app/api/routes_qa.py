"""知识库问答接口（检索即答）。

定位见 `services/knowledge_qa.py`：这是"先问一句"的轻量入口，
不需要严谨结论时用它；需要冲突裁决/人工审核时用「新建诊断」。

权限行为与其它接口保持一致：
- 显式请求无权知识库 → 403（不返回空结果）；
- 问题命中超出密级的指标域 → 403；
- 检索本身只返回当前用户可见且在其密级内的切片。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.qa import QaConversation, QaMessage
from app.schemas.qa import QaAskRequest
from app.services.audit import record_audit
from app.services.knowledge_qa import QaPermissionError, answer_question
from app.services.qa_conversation import (
    append_message,
    backfill_legacy_history,
    create_conversation,
    get_owned_conversation,
    recent_context,
)

router = APIRouter(prefix="/qa", tags=["知识库问答"])


@router.get("/history", summary="读取当前用户的知识库问答记录")
def history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("task:read")
    if backfill_legacy_history(
        session,
        tenant_id=principal.tenant_id,
        user_id=principal.user.id,
    ):
        session.commit()
    query = session.query(QaMessage).filter(
        QaMessage.tenant_id == principal.tenant_id,
        QaMessage.user_id == principal.user.id,
    )
    total = query.count()
    rows = (
        query.order_by(QaMessage.created_at.desc(), QaMessage.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [
            {
                "id": row.id,
                "conversation_id": row.conversation_id,
                "query": row.query,
                "answer": row.answer or {},
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/conversations", summary="读取当前用户的知识库问答会话")
def conversations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("task:read")
    if backfill_legacy_history(
        session,
        tenant_id=principal.tenant_id,
        user_id=principal.user.id,
    ):
        session.commit()
    query = session.query(QaConversation).filter(
        QaConversation.tenant_id == principal.tenant_id,
        QaConversation.user_id == principal.user.id,
    )
    total = query.count()
    rows = (
        query.order_by(QaConversation.last_message_at.desc(), QaConversation.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    conversation_ids = [row.id for row in rows]
    message_counts: dict[str, int] = {}
    latest_queries: dict[str, str] = {}
    if conversation_ids:
        stats = (
            session.query(
                QaMessage.conversation_id.label("conversation_id"),
                func.count(QaMessage.id).label("message_count"),
                func.max(QaMessage.sequence).label("max_sequence"),
            )
            .filter(
                QaMessage.conversation_id.in_(conversation_ids),
                QaMessage.tenant_id == principal.tenant_id,
                QaMessage.user_id == principal.user.id,
            )
            .group_by(QaMessage.conversation_id)
            .subquery()
        )
        summaries = (
            session.query(stats.c.conversation_id, stats.c.message_count, QaMessage.query)
            .join(
                QaMessage,
                and_(
                    QaMessage.conversation_id == stats.c.conversation_id,
                    QaMessage.sequence == stats.c.max_sequence,
                    QaMessage.tenant_id == principal.tenant_id,
                    QaMessage.user_id == principal.user.id,
                ),
            )
            .all()
        )
        message_counts = {conversation_id: int(count) for conversation_id, count, _ in summaries}
        latest_queries = {conversation_id: query_text for conversation_id, _, query_text in summaries}
    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "title": row.title,
                "message_count": message_counts.get(row.id, 0),
                "last_query": latest_queries.get(row.id, ""),
                "created_at": row.created_at.isoformat(),
                "updated_at": row.last_message_at.isoformat(),
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/conversations/{conversation_id}/messages", summary="读取会话的全部问答")
def conversation_messages(
    conversation_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("task:read")
    conversation = get_owned_conversation(
        session,
        conversation_id=conversation_id,
        tenant_id=principal.tenant_id,
        user_id=principal.user.id,
    )
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "conversation_not_found", "message": "会话不存在或无权访问"},
        )
    query = session.query(QaMessage).filter(QaMessage.conversation_id == conversation.id)
    total = query.count()
    rows = (
        query.order_by(QaMessage.sequence.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "conversation": {
            "id": conversation.id,
            "title": conversation.title,
            "created_at": conversation.created_at.isoformat(),
            "updated_at": conversation.last_message_at.isoformat(),
        },
        "items": [
            {
                "id": row.id,
                "conversation_id": row.conversation_id,
                "sequence": row.sequence,
                "query": row.query,
                "answer": row.answer or {},
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/ask", summary="知识库问答（检索即答，带引用）")
def ask(
    payload: QaAskRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    principal.require("task:read")
    conversation = None
    conversation_context: list[dict] = []
    if payload.conversation_id:
        conversation = get_owned_conversation(
            session,
            conversation_id=payload.conversation_id,
            tenant_id=principal.tenant_id,
            user_id=principal.user.id,
        )
        if conversation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "conversation_not_found", "message": "会话不存在或无权访问"},
            )
        conversation_context = recent_context(session, conversation_id=conversation.id)
    try:
        answer = answer_question(
            session,
            query=payload.query,
            tenant_id=principal.tenant_id,
            user_id=principal.user.id,
            roles=principal.roles,
            clearance=principal.user.security_level,
            allowed_kb_codes=sorted(principal.knowledge_bases),
            requested_kb_codes=payload.knowledge_bases,
            app_version=payload.app_version,
            date_from=payload.date_from,
            date_to=payload.date_to,
            top_k=payload.top_k,
            use_model=payload.use_model,
            conversation_context=conversation_context,
        )
    except QaPermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    answer_payload = answer.as_dict()
    if conversation is None:
        conversation = create_conversation(
            session,
            tenant_id=principal.tenant_id,
            user_id=principal.user.id,
            first_query=payload.query,
        )
    message = append_message(
        session,
        conversation=conversation,
        query=payload.query,
        answer=answer_payload,
        request_options={
            "knowledge_bases": payload.knowledge_bases,
            "app_version": payload.app_version,
            "date_from": payload.date_from.isoformat() if payload.date_from else None,
            "date_to": payload.date_to.isoformat() if payload.date_to else None,
            "top_k": payload.top_k,
            "use_model": payload.use_model,
        },
    )

    # 问答也要留痕：审计与聊天持久化分开，审计失败不影响会话主流程。
    audit = record_audit(
        session,
        action="knowledge_qa",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        resource_type="knowledge_base",
        resource_id=",".join(answer.knowledge_bases) or None,
        detail={
            "query": payload.query[:200],
            "citations": len(answer.citations),
            "insufficient": answer.insufficient,
            "mode": answer.mode,
            "conversation_id": conversation.id,
            "message_id": message.id,
            "answer": answer_payload,
        },
    )
    if audit is not None:
        message.source_audit_id = audit.id
    session.commit()
    return {
        **answer_payload,
        "conversation_id": conversation.id,
        "message_id": message.id,
        "created_at": message.created_at.isoformat(),
    }

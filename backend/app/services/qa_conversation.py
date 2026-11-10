"""知识库问答会话持久化、用户隔离与旧历史迁移。"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.qa import QaConversation, QaMessage
from app.models.workflow import AuditLog


def conversation_title(query: str, *, max_chars: int = 40) -> str:
    title = " ".join((query or "").split()).strip()
    if not title:
        return "新会话"
    return title if len(title) <= max_chars else title[:max_chars].rstrip() + "…"


def get_owned_conversation(
    session: Session,
    *,
    conversation_id: str,
    tenant_id: str,
    user_id: str,
) -> QaConversation | None:
    return (
        session.query(QaConversation)
        .filter(
            QaConversation.id == conversation_id,
            QaConversation.tenant_id == tenant_id,
            QaConversation.user_id == user_id,
        )
        .one_or_none()
    )


def create_conversation(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    first_query: str,
) -> QaConversation:
    row = QaConversation(
        tenant_id=tenant_id,
        user_id=user_id,
        title=conversation_title(first_query),
        last_message_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def append_message(
    session: Session,
    *,
    conversation: QaConversation,
    query: str,
    answer: dict,
    request_options: dict,
) -> QaMessage:
    sequence = (
        session.query(func.max(QaMessage.sequence))
        .filter(QaMessage.conversation_id == conversation.id)
        .scalar()
        or 0
    ) + 1
    row = QaMessage(
        conversation_id=conversation.id,
        tenant_id=conversation.tenant_id,
        user_id=conversation.user_id,
        sequence=sequence,
        query=query,
        answer=answer,
        request_options=request_options,
    )
    conversation.last_message_at = utcnow()
    session.add(row)
    session.flush()
    return row


def recent_context(
    session: Session,
    *,
    conversation_id: str,
    limit: int = 4,
) -> list[dict]:
    rows = (
        session.query(QaMessage)
        .filter(QaMessage.conversation_id == conversation_id)
        .order_by(QaMessage.sequence.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "query": row.query,
            "answer": str((row.answer or {}).get("answer") or ""),
        }
        for row in reversed(rows)
    ]


def backfill_legacy_history(session: Session, *, tenant_id: str, user_id: str) -> int:
    """把旧审计快照幂等转换成单消息会话，不修改原审计记录。"""
    existing_ids = {
        value
        for (value,) in session.query(QaMessage.source_audit_id)
        .filter(
            QaMessage.tenant_id == tenant_id,
            QaMessage.user_id == user_id,
            QaMessage.source_audit_id.is_not(None),
        )
        .all()
        if value
    }
    rows = (
        session.query(AuditLog)
        .filter(
            AuditLog.tenant_id == tenant_id,
            AuditLog.actor_id == user_id,
            AuditLog.action == "knowledge_qa",
            AuditLog.detail["answer"].is_not(None),
        )
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .all()
    )
    created = 0
    for audit in rows:
        if audit.id in existing_ids:
            continue
        detail = audit.detail or {}
        answer = detail.get("answer")
        if not isinstance(answer, dict) or not answer:
            continue
        query = str(answer.get("query") or detail.get("query") or "历史问答").strip()
        conversation = QaConversation(
            tenant_id=tenant_id,
            user_id=user_id,
            title=conversation_title(query),
            last_message_at=audit.created_at,
            created_at=audit.created_at,
            updated_at=audit.created_at,
        )
        session.add(conversation)
        session.flush()
        session.add(
            QaMessage(
                conversation_id=conversation.id,
                tenant_id=tenant_id,
                user_id=user_id,
                sequence=1,
                query=query,
                answer=answer,
                request_options={},
                source_audit_id=audit.id,
                created_at=audit.created_at,
                updated_at=audit.created_at,
            )
        )
        created += 1
    if created:
        session.flush()
    return created


__all__ = [
    "append_message",
    "backfill_legacy_history",
    "conversation_title",
    "create_conversation",
    "get_owned_conversation",
    "recent_context",
]

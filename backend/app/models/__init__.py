"""ORM 模型统一导出，保证 Alembic 与 create_all 都能发现全部表。"""

from app.models.base import Base
from app.models.evaluation import EvaluationCase, EvaluationRun
from app.models.identity import AccessPolicy, Role, Tenant, User, UserRole
from app.models.knowledge import Document, DocumentChunk, DocumentVersion, KnowledgeBase
from app.models.metrics import MetricDefinition, MetricImport, MetricSnapshot
from app.models.queue import TaskQueueItem
from app.models.qa import QaConversation, QaMessage
from app.models.scheduler import SchedulerLease
from app.models.ticket import (
    OrderFact,
    PolicyDocument,
    ReplyTemplate,
    Recommendation,
    Ticket,
    TicketMessage,
)
from app.models.workflow import (
    AgentRun,
    AnalysisEvent,
    AnalysisTask,
    Approval,
    AuditLog,
    EvidenceCitation,
    OperationalAlert,
    OperationalAlertDelivery,
    ReflectionReleaseState,
    Report,
    TaskExecutionUnit,
    ToolCall,
)

__all__ = [
    "Base",
    "AccessPolicy",
    "AgentRun",
    "AnalysisEvent",
    "AnalysisTask",
    "Approval",
    "AuditLog",
    "Document",
    "DocumentChunk",
    "DocumentVersion",
    "EvaluationCase",
    "EvaluationRun",
    "EvidenceCitation",
    "KnowledgeBase",
    "MetricDefinition",
    "MetricImport",
    "MetricSnapshot",
    "OrderFact",
    "OperationalAlert",
    "OperationalAlertDelivery",
    "PolicyDocument",
    "QaConversation",
    "QaMessage",
    "ReplyTemplate",
    "Recommendation",
    "ReflectionReleaseState",
    "Report",
    "Role",
    "SchedulerLease",
    "TaskQueueItem",
    "TaskExecutionUnit",
    "Tenant",
    "Ticket",
    "TicketMessage",
    "ToolCall",
    "User",
    "UserRole",
]

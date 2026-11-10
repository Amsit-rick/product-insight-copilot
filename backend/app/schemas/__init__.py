"""API 契约层（Pydantic v2）。

分层约定：
- `app/schemas` 只描述对外 HTTP 契约，不承载业务逻辑；
- 内部服务与 Agent 之间的契约放在各自模块（如 `app/agents/schemas.py`）；
- 所有响应都可以直接 JSON 序列化，不泄露 ORM 对象。
"""

from app.schemas.approval import ApprovalDecision, ApprovalOut
from app.schemas.auth import LoginRequest, MeOut, SessionResponse, TokenResponse, UserOut
from app.schemas.common import ErrorResponse, ListResponse, PageMeta
from app.schemas.document import (
    DocumentOut,
    DocumentUploadResponse,
    KnowledgeBaseOut,
    ReindexRequest,
    ReindexResponse,
)
from app.schemas.evaluation import EvaluationCaseOut, EvaluationRunOut, EvaluationRunRequest
from app.schemas.metric import (
    MetricDefinitionOut,
    MetricImportResponse,
    MetricQueryResponse,
)
from app.schemas.report import CitationOut, ReportOut
from app.schemas.task import (
    TaskApproveRequest,
    TaskCreateRequest,
    TaskEventOut,
    TaskOut,
    TaskRetryRequest,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalOut",
    "CitationOut",
    "DocumentOut",
    "DocumentUploadResponse",
    "ErrorResponse",
    "EvaluationCaseOut",
    "EvaluationRunOut",
    "EvaluationRunRequest",
    "KnowledgeBaseOut",
    "ListResponse",
    "LoginRequest",
    "MeOut",
    "SessionResponse",
    "MetricDefinitionOut",
    "MetricImportResponse",
    "MetricQueryResponse",
    "PageMeta",
    "ReindexRequest",
    "ReindexResponse",
    "ReportOut",
    "TaskApproveRequest",
    "TaskCreateRequest",
    "TaskEventOut",
    "TaskOut",
    "TaskRetryRequest",
    "TokenResponse",
    "UserOut",
]
from app.schemas.reflection import (
    DiagnosticReportDraft,
    ReflectionIssue,
    ReflectionResult,
    ReportRevision,
    TicketReplyRevision,
)

__all__ = [
    "DiagnosticReportDraft",
    "ReflectionIssue",
    "ReflectionResult",
    "ReportRevision",
    "TicketReplyRevision",
]

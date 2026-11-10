"""售后工单接口的请求/响应模型（需求书 §6 API 契约）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import TicketChannel, TicketPriority


def _required_text(value: str) -> str:
    """Normalize user-entered text before persistence and reject whitespace-only values."""
    normalized = value.strip()
    if not normalized:
        raise ValueError("内容不能为空")
    return normalized


class TicketCreateRequest(BaseModel):
    """创建工单。`external_id` 与 `idempotency_key` 二者之一存在时按幂等处理。"""

    model_config = ConfigDict(extra="allow")

    subject: str = Field(min_length=2, max_length=255, description="问题摘要（列表页展示）")
    content: str = Field(min_length=2, max_length=20000, description="用户原话")
    external_id: str | None = Field(default=None, max_length=64, description="来源系统工单号")
    customer_id: str | None = Field(default=None, max_length=64)
    order_id: str | None = Field(default=None, max_length=64)
    channel: TicketChannel = TicketChannel.MANUAL
    priority: TicketPriority = TicketPriority.MEDIUM
    issue_type: str | None = None
    shop_id: str | None = None
    platform: str | None = None
    attributes: dict = Field(default_factory=dict)
    data_origin: Literal["business", "demo", "test"] = Field(
        default="business",
        description="工单数据来源；自动化测试必须显式标记为 test",
    )
    idempotency_key: str | None = Field(default=None, max_length=128)

    _normalize_text = field_validator("subject", "content", mode="after")(_required_text)


class TicketTransitionRequest(BaseModel):
    target: str
    reason: str | None = Field(default=None, max_length=500)


class TicketAssignRequest(BaseModel):
    assignee_id: str | None = None
    reason: str | None = Field(default=None, max_length=500)


class TicketOrderUpdateRequest(BaseModel):
    """客服在原工单补录订单号，补录后才能查询真实订单事实。"""

    order_id: str = Field(min_length=1, max_length=64)


class AssignmentCandidateOut(BaseModel):
    user_id: str
    username: str
    full_name: str | None = None
    department: str | None = None
    can_approve: bool = False
    active_ticket_count: int = 0
    high_priority_count: int = 0
    waiting_review_count: int = 0
    score: int = 0
    reasons: list[str] = Field(default_factory=list)


class AssignmentRecommendationsOut(BaseModel):
    ticket_id: str
    current_assignee_id: str | None = None
    issue_type_context: str | None = None
    rule_version: str
    items: list[AssignmentCandidateOut] = Field(default_factory=list)


class TicketReplyRequest(BaseModel):
    """客服发送回复（可带修改后的正文）。"""

    content: str = Field(min_length=1, max_length=10000)
    visible_to_customer: bool = True
    message_type: str = Field(default="reply", pattern="^(reply|internal_note)$")
    mention_user_ids: list[str] = Field(default_factory=list, max_length=10)
    idempotency_key: str | None = None
    # 质检结果只允许写入内部消息，不会触发客户回复或工单完成。
    quality_review: bool = False
    quality_result: str | None = Field(default=None, pattern="^(pass|needs_followup|block)$")
    quality_issue_codes: list[str] = Field(default_factory=list, max_length=10)


class RecommendationDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str | None = Field(default=None, max_length=500, description="审批理由（L3 必填）")
    modified_reply: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)


class RecommendationSendRequest(BaseModel):
    content: str | None = Field(default=None, max_length=10000)
    idempotency_key: str | None = Field(default=None, max_length=128)


class ReplyTemplateCreateRequest(BaseModel):
    title: str = Field(min_length=2, max_length=100)
    content: str = Field(min_length=2, max_length=5000)
    issue_type: str | None = Field(default=None, max_length=32)

    _normalize_text = field_validator("title", "content", mode="after")(_required_text)


class ReplyTemplateUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=100)
    content: str | None = Field(default=None, min_length=2, max_length=5000)
    issue_type: str | None = Field(default=None, max_length=32)
    active: bool | None = None

    _normalize_text = field_validator("title", "content", mode="after")(
        lambda value: None if value is None else _required_text(value)
    )


class ReplyTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    title: str
    content: str
    issue_type: str | None = None
    active: bool
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime
    updated_at: datetime


class TicketMentionOut(BaseModel):
    id: str
    ticket_id: str
    ticket_external_id: str | None = None
    subject: str
    content: str
    actor_id: str | None = None
    created_at: datetime
    read: bool = False
    mention_user_ids: list[str] = Field(default_factory=list)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sender_type: str
    actor_id: str | None = None
    content: str
    visible_to_customer: bool
    meta: dict = Field(default_factory=dict)
    created_at: datetime


class FactOut(BaseModel):
    """事实快照。前端要标注"来源 + 查询时间 + 是否模拟"（FR-02）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    fact_type: str
    payload: dict
    observed_at: datetime | None = None
    request_id: str | None = None
    is_mock: bool
    adapter: str | None = None
    stale: bool = False


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    external_id: str | None = None
    customer_id: str | None = None
    order_id: str | None = None
    shop_id: str | None = None
    platform: str | None = None
    subject: str
    content: str
    channel: str
    status: str
    issue_type: str | None = None
    priority: str
    sentiment: str | None = None
    risk_tier: str | None = None
    assignee_id: str | None = None
    last_analysis_task_id: str | None = None
    analysis_count: int = 0
    missing_fields: list = Field(default_factory=list)
    attributes: dict = Field(default_factory=dict)
    data_origin: Literal["business", "demo", "test"] = "business"
    escalation_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    # 允许的操作由后端状态机给出，前端不该自己猜（避免点了按钮才报错）
    allowed_transitions: list[str] = Field(default_factory=list)
    # 客服工作台字段：由后端计算，前端不自行推断 SLA 或下一步动作
    sla_due_at: str | None = None
    sla_remaining_seconds: int | None = None
    sla_status: str = "on_track"
    next_action: str = "查看工单"
    handler_label: str = "未分配"
    # 主管质检摘要：由最新的客户不可见质检记录计算，列表页不展示技术细节。
    quality_status: str = "unreviewed"
    quality_issue_codes: list[str] = Field(default_factory=list)
    quality_reviewed_at: datetime | None = None
    quality_reviewer_id: str | None = None


class RecommendationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    ticket_id: str
    analysis_task_id: str | None = None
    action_type: str
    amount: Decimal | None = None
    # 金额来源（"支付事实（实付金额）"/"政策条款（超时补偿标准）"）：审批人据此判断金额是否可信
    amount_source: str | None = None
    currency: str = "CNY"
    rationale: str | None = None
    evidence_ids: list = Field(default_factory=list)
    policy_refs: list = Field(default_factory=list)
    missing_information: list = Field(default_factory=list)
    risk_tier: str
    risk_reason: str | None = None
    approval_requirement: str
    confidence: float | None = None
    status: str
    customer_reply: str | None = None
    internal_note: str | None = None
    warnings: list = Field(default_factory=list)
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None
    original_reply: str | None = None
    created_at: datetime
    # 便于前端直接渲染"这个建议该谁审、能不能自动执行"
    approval_label: str | None = None
    auto_draft_allowed: bool = True
    write_action_blocked: bool = False
    # 业务语言门禁，替代客服直接阅读置信度小数
    answerability_code: str = "ready_to_reply"
    answerability_label: str = "可以直接处理"
    answerability_reason: str | None = None
    reply_commitment_blocked: bool = False


class TicketBusinessStepOut(BaseModel):
    agent_code: str
    role: str
    responsibility: str
    task: str
    why: str
    phase: str
    parallel_group: str | None = None
    status: str
    conclusion: str


class TicketAnalysisOut(BaseModel):
    mode: Literal["fast_path", "multi_agent"] | None = None
    mode_label: str | None = None
    route_reason: str
    agent_count: int = 0
    business_steps: list[TicketBusinessStepOut] = Field(default_factory=list)
    applied_skills: list[str] = Field(default_factory=list)
    analysis_task_id: str | None = None
    analysis_count: int = 0
    steps: list[dict] = Field(default_factory=list)
    confidence: float | None = None
    risk_tier: str | None = None
    reflection: dict = Field(default_factory=dict)


class TicketDetailOut(BaseModel):
    ticket: TicketOut
    messages: list[MessageOut] = Field(default_factory=list)
    facts: list[FactOut] = Field(default_factory=list)
    recommendation: RecommendationOut | None = None
    # 原型左栏是"用户与订单"6 行键值：逐单变化，所以用 {key,value}[] 而不是固定 schema
    fact_rows: list[dict] = Field(default_factory=list)
    timeline: list[dict] = Field(default_factory=list)
    analysis: TicketAnalysisOut | None = None
    handoff_summary: dict | None = None
    # 本单成本（累计）：需求书 §8 可观测要求，也是"能不能用在高频"的算术依据
    cost: dict | None = None


class TicketStatsOut(BaseModel):
    today_new: int = 0
    pending: int = 0
    pending_high_priority: int = 0
    waiting_review: int = 0
    avg_first_response_minutes: float | None = None
    ai_acceptance_rate: float | None = None
    ai_decided_count: int = 0
    waiting_customer: int = 0
    waiting_reply: int = 0
    at_risk: int = 0
    resolved_today: int = 0
    excluded_test_records: int = 0
    issue_type_options: list[str] = Field(default_factory=list)
    status_options: list[str] = Field(default_factory=list)


class TicketStateMachineOut(BaseModel):
    transitions: dict
    terminal: list[str]

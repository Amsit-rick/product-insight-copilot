"""电商售后工单领域模型（需求书 §5 数据模型）。

## 与既有平台的衔接（三条设计决策，都有理由）

1. **一次分析 = 一个 `AnalysisTask`**。工单是业务实体，分析是执行动作，两者分离：
   `Ticket.last_analysis_task_id` 指向最近一次分析运行。这样执行栈（LangGraph 运行器、
   `analysis_events` 事件、`agent_runs` 轨迹与 token 台账、审批、成本核算、审计）
   **一行都不用重写**，而且"同一工单重新分析"天然就是新建一次 task（历史可追溯）。

2. **政策文档不另建切片/向量表**：`PolicyDocument.document_id` 指向既有 `documents`，
   切片与向量仍走 `document_chunks` + 三档向量库。政策只是文档的**一种带生效期的语义**，
   复制一套切片/向量表会让检索实现分叉（那是本项目踩过的坑：同一个问题两套口径）。

3. **金额用 `Numeric(12, 2)` 而不是浮点**：需求书明确"不以模型自由生成的金额作为最终财务依据"，
   所以金额字段必须是精确十进制，且只能由规则/适配器写入，模型只能引用。

风险分级（L1~L4）落在 `Recommendation.risk_tier`，并由 `approval_requirement` 明确
"谁能拍板"——把"风险等级"与"该谁审"分开存，是因为同一个 L3 建议在不同租户可能有不同审批路径。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base, TimestampMixin, UUIDPkMixin
from app.models.enums import (
    ApprovalRequirement,
    FactSource,
    IssueType,
    MessageSender,
    PolicyStatus,
    RecommendationStatus,
    RiskTier,
    Sentiment,
    TicketChannel,
    TicketPriority,
    TicketStatus,
)


class Ticket(UUIDPkMixin, TimestampMixin, Base):
    """售后工单。状态迁移由后端校验（`services/ticket_state.py`），前端不能直接改写。"""

    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_ticket_tenant_status", "tenant_id", "status"),
        Index("ix_ticket_tenant_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # 来源系统的工单号：幂等创建的依据（同一 external_id 重复提交不得产生第二张工单）
    external_id: Mapped[str | None] = mapped_column(String(64), index=True)
    customer_id: Mapped[str | None] = mapped_column(String(64), index=True)
    order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    shop_id: Mapped[str | None] = mapped_column(String(64), index=True)
    platform: Mapped[str | None] = mapped_column(String(32), index=True)

    subject: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(32), default=TicketChannel.MANUAL.value)
    status: Mapped[str] = mapped_column(
        String(32), default=TicketStatus.NEW.value, index=True
    )
    issue_type: Mapped[str | None] = mapped_column(String(32), index=True)
    priority: Mapped[str] = mapped_column(String(16), default=TicketPriority.MEDIUM.value)
    sentiment: Mapped[str | None] = mapped_column(String(16))
    risk_tier: Mapped[str | None] = mapped_column(String(4))

    assignee_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_by: Mapped[str | None] = mapped_column(String(36))

    # 最近一次分析运行（AnalysisTask.id）；重新分析 = 新建一次运行，历史不被覆盖
    last_analysis_task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    analysis_count: Mapped[int] = mapped_column(Integer, default=0)

    # 结构化补充信息（平台/店铺/会员等级/金额等），避免为每个渠道加列
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    escalation_reason: Mapped[str | None] = mapped_column(String(255))
    missing_fields: Mapped[list] = mapped_column(JSON, default=list)

    # 当前质检摘要用于列表筛选和分页；完整质检历史仍保存在 ticket_messages。
    quality_status: Mapped[str] = mapped_column(String(24), default="unreviewed", index=True, nullable=False)
    quality_issue_codes: Mapped[list] = mapped_column(JSON, default=list)
    quality_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    quality_reviewer_id: Mapped[str | None] = mapped_column(String(36))

    idempotency_key: Mapped[str | None] = mapped_column(String(128), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)


class TicketMessage(UUIDPkMixin, TimestampMixin, Base):
    """工单消息（客户诉求、AI 草稿、客服实际回复、系统提示）。

    草稿与"实际发出"必须分开存：需求书 FR-04 要求"提交前保留修改记录和原始草稿"。
    """

    __tablename__ = "ticket_messages"
    __table_args__ = (
        Index("ix_message_ticket_created", "ticket_id", "created_at"),
        UniqueConstraint("ticket_id", "idempotency_key", name="uq_ticket_message_idempotency"),
    )

    ticket_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    sender_type: Mapped[str] = mapped_column(String(16), default=MessageSender.CUSTOMER.value)
    actor_id: Mapped[str | None] = mapped_column(String(36))
    content: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    visible_to_customer: Mapped[bool] = mapped_column(Boolean, default=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), index=True)


class ReplyTemplate(UUIDPkMixin, TimestampMixin, Base):
    """租户级常用回复模板；模板只负责起草，发送仍走工单回复门禁。"""

    __tablename__ = "ticket_reply_templates"
    __table_args__ = (Index("ix_reply_template_tenant_active", "tenant_id", "active"),)

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    issue_type: Mapped[str | None] = mapped_column(String(32), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36))
    updated_by: Mapped[str | None] = mapped_column(String(36))


class OrderFact(UUIDPkMixin, TimestampMixin, Base):
    """订单/支付/物流/售后事实快照。

    需求书 FR-02 要求"每个字段标明来源、查询时间和是否实时"，所以一条事实不是裸 JSON，
    而是带 `source / observed_at / request_id / is_mock` 的快照：
    - `observed_at`：接口返回时间 → 判断"事实是否过期"的依据；
    - `request_id`：可回查是哪次接口调用产生的事实（审计与争议排查用）；
    - `is_mock`：MVP 用模拟适配器，界面上必须能一眼看出（防止把模拟当生产）。
    """

    __tablename__ = "order_facts"
    __table_args__ = (Index("ix_fact_ticket_source", "ticket_id", "source"),)

    ticket_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default=FactSource.ORDER.value)
    fact_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=True)
    adapter: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[dict | None] = mapped_column(JSON)


class Recommendation(UUIDPkMixin, TimestampMixin, Base):
    """处理建议：动作 + 金额 + 依据（证据 ID / 政策条款）+ 风险等级 + 回复草稿。

    MVP 纪律（需求书 §1.2 / §4.3）：
    - 金额字段只能由规则与适配器写入，模型**只能引用**不能计算；
    - 写操作类动作（退款/补发/赔付）一律不允许自动执行，必须有人审批；
    - `evidence_ids` / `policy_refs` 为空时，界面必须显示"无证据"而不是空白。
    """

    __tablename__ = "recommendations"
    __table_args__ = (
        Index("ix_reco_ticket_status", "ticket_id", "status"),
        Index("ix_reco_analysis_task", "analysis_task_id"),
        UniqueConstraint("ticket_id", "analysis_task_id", name="uq_reco_ticket_analysis_task"),
    )

    ticket_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    analysis_task_id: Mapped[str | None] = mapped_column(String(36))

    action_type: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # 金额来源：审批人要能一眼看出"这笔钱是从哪来的"。规则节点本来就区分
    # "支付事实（实付金额）"（退款引用实付）与"政策条款（超时补偿标准）"（补偿引用政策），
    # 但以前只写进 rationale 的散文里、没落库，界面上看不到，事后也无法按来源统计。
    amount_source: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    rationale: Mapped[str | None] = mapped_column(Text)

    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    policy_refs: Mapped[list] = mapped_column(JSON, default=list)
    missing_information: Mapped[list] = mapped_column(JSON, default=list)

    risk_tier: Mapped[str] = mapped_column(String(4), default=RiskTier.L1.value, index=True)
    # 风险原因单独存：原型把两者拼成一个字符串（"L3 · 需要主管审批"），
    # 前端就无法做色卡/排序/筛选。等级与原因必须分开，展示层再拼。
    risk_reason: Mapped[str | None] = mapped_column(String(255))
    approval_requirement: Mapped[str] = mapped_column(
        String(16), default=ApprovalRequirement.NONE.value
    )
    confidence: Mapped[float | None] = mapped_column()
    status: Mapped[str] = mapped_column(
        String(24), default=RecommendationStatus.DRAFT.value, index=True
    )

    customer_reply: Mapped[str | None] = mapped_column(Text)
    internal_note: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list] = mapped_column(JSON, default=list)

    decided_by: Mapped[str | None] = mapped_column(String(36))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    # 修改后通过：保留原始草稿与改动 diff（FR-04 / FR-05）
    original_reply: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), index=True)


class PolicyDocument(UUIDPkMixin, TimestampMixin, Base):
    """售后政策文档的**生效期语义**（需求书 §4.3：检索必须过滤 policy_version + effective_at）。

    切片与向量不在这里：`document_id` 指向既有 `documents`，命中 `document_chunks` 与向量库。
    但这张表是"能不能引用这条政策"的唯一依据 —— 政策有版本和生效期，
    引用过期政策是合规事故，所以过滤必须落在检索层而不是展示层。
    """

    __tablename__ = "policy_documents"
    __table_args__ = (
        Index("ix_policy_tenant_scope", "tenant_id", "status"),
        Index("ix_policy_effective", "effective_from", "effective_to"),
    )

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255))
    version: Mapped[str] = mapped_column(String(32))
    # 适用范围：{"platform": "...", "shop_id": "...", "category": "...", "member_level": "..."}
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    effective_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default=PolicyStatus.ACTIVE.value)
    source_uri: Mapped[str | None] = mapped_column(String(512))
    document_id: Mapped[str | None] = mapped_column(String(36), index=True)
    kb_code: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)

"""全局枚举定义。

使用 str 枚举，落库为字符串，避免不同数据库对原生 enum 类型的差异。
"""

from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING_HUMAN = "waiting_human"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"


class ApprovalAction(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"


class KnowledgeBaseCode(str, Enum):
    PRODUCT = "product_kb"
    FEEDBACK = "feedback_kb"
    ENGINEERING = "engineering_kb"
    OPERATION = "operation_kb"
    # 售后政策库：需求书 §4.3 要求按 policy_version + effective_at 过滤检索，
    # 所以政策必须是独立知识库（不能混在运营库里，否则生效期语义无从表达）。
    POLICY = "policy_kb"


class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    INDEXING = "indexing"
    INDEXED = "indexed"
    FAILED = "failed"
    DELETED = "deleted"


class SecurityLevel(str, Enum):
    """密级：数值越大越敏感，检索时按用户密级上限过滤。"""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"

    @property
    def rank(self) -> int:
        return {
            SecurityLevel.PUBLIC: 0,
            SecurityLevel.INTERNAL: 1,
            SecurityLevel.CONFIDENTIAL: 2,
            SecurityLevel.SECRET: 3,
        }[self]


class RoleCode(str, Enum):
    ADMIN = "admin"
    PRODUCT_MANAGER = "product_manager"
    OPERATION = "operation"
    ENGINEER = "engineer"
    REVIEWER = "reviewer"


class IntentCode(str, Enum):
    METRIC_ANOMALY = "metric_anomaly"
    VERSION_IMPACT = "version_impact"
    FEEDBACK_CLUSTER = "feedback_cluster"
    REQUIREMENT_PRIORITY = "requirement_priority"
    GENERAL_QA = "general_qa"


class EvidenceKind(str, Enum):
    DOCUMENT = "document"
    METRIC = "metric"


class StatementType(str, Enum):
    """报告陈述类型：事实 / 推断 / 假设 / 建议。"""

    FACT = "fact"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"
    RECOMMENDATION = "recommendation"


class AgentCode(str, Enum):
    PRODUCT = "product_agent"
    FEEDBACK = "feedback_agent"
    RELEASE = "release_agent"
    METRIC = "metric_agent"
    REPORT = "report_agent"
    # 电商售后工单的 7 个 Agent（需求书 §3.2）
    TRIAGE = "triage_agent"
    ORDER_FACT = "order_fact_agent"
    POLICY_RAG = "policy_rag_agent"
    RESOLUTION = "resolution_agent"
    RESPONSE = "response_agent"
    REVIEW = "review_agent"
    FOLLOW_UP = "followup_agent"


class EventLevel(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class ImportStatus(str, Enum):
    PENDING = "pending"
    VALIDATING = "validating"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


# ===========================================================================
# 电商售后工单（《电商售后工单智能处理系统》需求书 §2 / §3）
# ===========================================================================
class TicketStatus(str, Enum):
    """工单状态机。合法迁移由后端校验（`services/ticket_state.py`），前端不能直接改写状态。"""

    NEW = "new"                                # 已受理，未补事实
    ENRICHING = "enriching"                    # 补齐订单/支付/物流事实
    ANALYZING = "analyzing"                    # Agent 分析中
    WAITING_REVIEW = "waiting_review"          # 等待人工审批（L3/L4 或低置信度）
    READY_TO_REPLY = "ready_to_reply"          # 草稿已就绪，客服可发送
    RESOLVED = "resolved"                      # 已回复/已处理（终态）

    # 异常态
    NEED_CUSTOMER_INFO = "need_customer_info"  # 缺订单号等必要信息，等客户补充
    ESCALATED = "escalated"                    # 升级（风控/主管专案）
    FAILED = "failed"                          # 处理失败（可重试回 ENRICHING）
    CANCELLED = "cancelled"                    # 已取消


class IssueType(str, Enum):
    """MVP 支持的问题类型（需求书 §2.1），供 Triage Agent 分类，并在评测里算 Macro-F1。"""

    REFUND = "refund"                                # 退款
    RETURN = "return"                                # 退货
    EXCHANGE = "exchange"                            # 换货
    LOGISTICS_STALLED = "logistics_stalled"          # 物流停滞
    MISSING_ITEM = "missing_item"                    # 少件
    DAMAGED = "damaged"                              # 破损
    WRONG_ITEM = "wrong_item"                        # 错发
    NOT_SHIPPED = "not_shipped"                      # 整单未发货（漏发商品归 missing_item）
    PROMOTION_NOT_APPLIED = "promotion_not_applied"  # 优惠未生效
    DESCRIPTION_MISMATCH = "description_mismatch"    # 商品与描述不符
    OTHER = "other"                                  # 兜底：无法归类（评测单独统计，不计入 10 类）


class RiskTier(str, Enum):
    """风险分级（需求书 §3.3）。L1 可自动出草稿；L3 需主管审批；L4 禁止 Agent 自动执行。"""

    L1 = "L1"  # 查询政策、生成草稿、金额为 0 的解释
    L2 = "L2"  # 补发、优惠券、低金额赔付：规则校验后需客服确认
    L3 = "L3"  # 退款、较高金额赔付、责任争议、敏感用户：主管审批并记录理由
    L4 = "L4"  # 批量退款、账户限制、疑似欺诈：转风控 / 人工专案


class ApprovalRequirement(str, Enum):
    """由风险分级推导的审批要求，写在建议上，便于前端与审核人直接读。"""

    NONE = "none"                    # L1：客服发送前确认即可
    STAFF_CONFIRM = "staff_confirm"  # L2：客服确认
    SUPERVISOR = "supervisor"        # L3：主管审批
    RISK_CONTROL = "risk_control"    # L4：转风控，禁止自动执行


class TicketPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TicketChannel(str, Enum):
    """来源渠道。MVP 先支持后台录入与 JSON API（需求书 §2.1）。"""

    MANUAL = "manual"
    API = "api"
    WECOM = "wecom"
    FEISHU = "feishu"
    CUSTOMER_SERVICE = "customer_service"


class Sentiment(str, Enum):
    CALM = "calm"
    NEUTRAL = "neutral"
    ANGRY = "angry"


class MessageSender(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"  # AI 生成的草稿
    STAFF = "staff"  # 客服实际发出的内容
    SYSTEM = "system"


class FactSource(str, Enum):
    """事实来源。Order Fact Agent 只能调只读接口，产出的事实必须带来源与观测时间。"""

    ORDER = "order"
    PAYMENT = "payment"
    LOGISTICS = "logistics"
    AFTERSALE = "aftersale"


class RecommendationStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    EXECUTED = "executed"
    CANCELLED = "cancelled"


class PolicyStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    EXPIRED = "expired"
    ARCHIVED = "archived"


class ActionType(str, Enum):
    """建议的处理动作。写操作类（refund/reship/compensate）在 MVP 不允许自动执行。"""

    REPLY_ONLY = "reply_only"
    ASK_CUSTOMER = "ask_customer"
    REFUND = "refund"
    RETURN_GOODS = "return_goods"
    EXCHANGE = "exchange"
    RESHIP = "reship"
    COMPENSATE = "compensate"
    COUPON = "coupon"
    CONTACT_LOGISTICS = "contact_logistics"
    ESCALATE = "escalate"
    REJECT = "reject"

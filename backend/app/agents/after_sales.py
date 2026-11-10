"""电商售后工单的 7 个 Agent（需求书 §3.2）。

    Triage Agent      问题分类：issue_type / urgency / sentiment / missing_fields / confidence
    Order Fact Agent  只读查询订单、支付、物流、售后事实（工具层强制只读，产出带时间戳）
    Policy RAG Agent  检索适用政策（按来源、版本、生效时间过滤）
    Resolution Agent  给出处理方案（**金额只能引用事实与政策，不得自行计算**）
    Response Agent    生成客服回复草稿（区分消费者可见内容与内部备注）
    Review Agent      质检：证据覆盖、政策冲突、金额一致性、是否升级
    Follow-up Agent   跟进计划：等待外部处理时创建提醒，超时升级

为什么都写 `deterministic_output`：需求书要求"Agent 输出必须使用 JSON Schema；
自然语言只作为展示层"。离线（stub 模型）与分析场景下，规则输出必须能独立成立 ——
模型只是把它写得更像人话，不能是唯一路径。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.schemas.reflection import ReflectionIssue

from app.agents.base import AgentOutputModel, BaseAgent, Collected, make_evidence
from app.agents.schemas import AgentInput
from app.config import get_settings
from app.models.enums import ActionType, IssueType, KnowledgeBaseCode, Sentiment
from app.tools.registry import ToolContext

# --------------------------------------------------------------------------- 分类规则
ISSUE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (IssueType.NOT_SHIPPED.value, ("没有发货", "未发货", "还没发货", "一直没发", "不发货")),
    (
        IssueType.LOGISTICS_STALLED.value,
        (
            "停滞",
            "没有更新",
            "不动了",
            "没动",
            "停了",
            "催件",
            "催一下",
            "催单",
            "超时",
            "没收到",
            "签收",
            "快递异常",
            "物流",
        ),
    ),
    (IssueType.MISSING_ITEM.value, ("少件", "少了一", "只收到一件", "缺件", "漏发", "漏了", "不全")),
    # 口语化破损说法（"瘪/变形/坏的"）是评测集扩到 200+ 条时暴露出来的缺口：
    # 用户说"外箱瘪了""压变形了""商品是坏的"时，旧关键词（外包装/破损/压坏/坏了/碎了）
    # 一个都不命中 → 被判成"未识别"→ 追问客户，而不是按政策直接补发。
    # 这三个词在售后语境里没有歧义（都指商品/包装受损），所以按最小化原则补进来。
    (IssueType.DAMAGED.value, ("破损", "碎了", "坏了", "坏的", "压坏", "瘪", "变形", "外包装")),
    (IssueType.WRONG_ITEM.value, ("错发", "发错", "不是我买", "型号不对", "颜色不对")),
    (IssueType.EXCHANGE.value, ("换货", "换一个", "换新")),
    (IssueType.RETURN.value, ("退货", "退回", "不想要了")),
    # "退给我"同样来自评测集的措辞变体："把这笔钱退给我"这类说法不含"退我"这个子串，
    # 旧关键词命中不了 → 退款诉求被判成"未识别"→ 动作退化成追问客户。
    (IssueType.REFUND.value, ("退款", "退钱", "退我", "退给我")),
    (
        IssueType.PROMOTION_NOT_APPLIED.value,
        (
            "满减",
            "优惠",
            "券",
            "没减",
            "没有减",
            "未减",
            "立减",
            "活动价",
            "活动",
            "门槛",
            "补贴",
            "能不能减",
        ),
    ),
    (IssueType.DESCRIPTION_MISMATCH.value, ("描述不符", "和图片不一样", "与描述", "宣传", "货不对板")),
)

# 促销文案有固定写法（"满 199 减 30"），关键词拆不开，用正则单独兜住
PROMOTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"满\s*\d+\s*减"),
    re.compile(r"满\s*\d+\s*立减"),
    re.compile(r"优惠(?:券|没|不)"),
)

URGENT_KEYWORDS = ("欺诈", "投诉", "消协", "工商", "曝光", "差评", "起诉", "315", "举报")
ANGRY_KEYWORDS = ("垃圾", "骗子", "气愤", "太差", "恶劣", "无法接受", "什么意思", "？！", "！！")
HIGH_RISK_KEYWORDS = ("批量", "全部订单", "多个订单", "账号", "限制", "冻结")


# 分类优先级：**现象优先于诉求**。
# 理由：用户说"不想要了，申请退款"时，退款是诉求、退货只是手段；
# 而用户说"只收到一件，要退货"时，少件才是要解决的问题（退货是解决方式）。
# 只按"最先出现的关键词"判会在这两种句子上判反（实测："商品不想要了，申请退款"被判成退货）。
ISSUE_PRIORITY: dict[str, int] = {
    IssueType.NOT_SHIPPED.value: 1,
    IssueType.LOGISTICS_STALLED.value: 2,
    IssueType.MISSING_ITEM.value: 3,
    IssueType.DAMAGED.value: 4,
    IssueType.WRONG_ITEM.value: 5,
    IssueType.PROMOTION_NOT_APPLIED.value: 6,
    IssueType.DESCRIPTION_MISMATCH.value: 7,
    IssueType.REFUND.value: 8,      # 诉求
    IssueType.RETURN.value: 9,      # 诉求（且常是手段）
    IssueType.EXCHANGE.value: 10,   # 诉求（且常是手段）
}

# 用户明确索要退款时的措辞（方案生成要尊重明确诉求）。
# "退给我"与"退我"不互为子串（"这笔钱退给我"里没有"退我"），所以两个都要在。
EXPLICIT_REFUND_WORDS = ("退款", "退钱", "退我", "退给我", "把钱退")


def _indicates_fulfillment(value: object) -> bool:
    """Return whether a normalized order/logistics value proves fulfillment started.

    Real adapters are allowed to return normalized English enum values, while the
    bundled mock uses Chinese labels.  Keep the safety rule bilingual and reject
    explicit negative states before looking for positive English tokens.
    """
    text = str(value or "").strip()
    if not text:
        return False
    if any(marker in text for marker in ("已发货", "运输中", "派送中", "已签收", "已完成", "交易成功")):
        return True

    normalized = text.lower().replace("-", " ").replace("_", " ")
    if any(
        marker in normalized
        for marker in ("not shipped", "unshipped", "pending shipment", "awaiting shipment")
    ):
        return False
    return bool(
        re.search(
            r"\b(shipped|in transit|out for delivery|delivered|signed|completed|fulfilled)\b",
            normalized,
        )
    )


def _indicates_not_received(text: object) -> bool:
    """客户明确说"没收到/未签收"——与已签收事实直接冲突。"""
    value = str(text or "")
    return any(
        marker in value
        for marker in ("没收到", "未收到", "没有收到", "没签收", "未签收", "没拿到", "未拿到")
    )


def decide_action(
    *,
    query: str,
    issue_type: str,
    facts: dict,
    policy_text: str = "",
) -> dict:
    """**受控决策**：按规则给出动作与金额（纯函数，模型不得改写）。

    为什么必须独立成规则函数（实测踩过的坑）：接了真实模型后，模型会在润色时把
    `action_type` 从 `refund` 改写成 `ask_customer`、把 `issue_type` 写成中文标签 ——
    后果是风险等级从 L3 掉到 L2，本该主管审批的退款变成了可自动发送的草稿。
    所以动作、金额、问题类型这三个字段一律由本函数决定，Agent 只负责把话说顺。

    金额同样只**引用**：退款取支付事实的实付金额，补偿取政策文本里写明的标准。
    """
    paid_amount = facts.get("paid_amount")
    stalled = facts.get("stalled_hours") or 0
    order_status = str(facts.get("order_status") or "").strip()
    latest_track = str(facts.get("latest_track") or "").strip()
    signed_at = str(facts.get("signed_at") or "").strip()
    missing: list[str] = []
    conflict = False

    # 客户口述只用于识别诉求，执行动作必须服从已核验事实。客户说“未发货”但
    # 平台已经显示发货/运输/签收时，直接补发会造成重复履约，必须先人工核实。
    fulfilled = (
        _indicates_fulfillment(order_status)
        or _indicates_fulfillment(latest_track)
        or bool(signed_at)
    )
    not_shipped_conflict = issue_type == IssueType.NOT_SHIPPED.value and fulfilled
    # 物流类问题里"已签收"与"客户说没收到"也是直接冲突：先核实，不能直接退款/补发。
    not_received_conflict = (
        issue_type == IssueType.LOGISTICS_STALLED.value
        and fulfilled
        and _indicates_not_received(query)
    )

    if not_shipped_conflict or not_received_conflict:
        action, amount = ActionType.ASK_CUSTOMER.value, None
        source = "订单事实与客户描述冲突"
        conflict = True
        if order_status:
            conflict_fact = f"订单状态为{order_status}"
        elif latest_track:
            conflict_fact = f"最新物流为{latest_track}"
        else:
            conflict_fact = f"存在签收时间 {signed_at}"
        if not_received_conflict:
            missing.append(f"实际签收情况（客户称未收到，但{conflict_fact}）")
        else:
            missing.append(f"实际履约情况（客户描述未发货，但{conflict_fact}）")
    elif wants_refund_explicitly(query) and paid_amount:
        action, amount = ActionType.REFUND.value, paid_amount
        source = "支付事实（实付金额）"
    elif wants_refund_explicitly(query):
        action, amount, source = ActionType.REFUND.value, None, None
        missing.append("实付金额")
    elif issue_type == IssueType.REFUND.value:
        action, amount = ActionType.REFUND.value, paid_amount
        source = "支付事实（实付金额）" if paid_amount else None
        if not paid_amount:
            missing.append("实付金额")
    elif issue_type in (IssueType.RETURN.value, IssueType.EXCHANGE.value):
        action = (
            ActionType.RETURN_GOODS.value
            if issue_type == IssueType.RETURN.value
            else ActionType.EXCHANGE.value
        )
        amount, source = "0", "规则：退换货不涉及赔付"
    elif issue_type in (
        IssueType.MISSING_ITEM.value,
        IssueType.NOT_SHIPPED.value,
        IssueType.WRONG_ITEM.value,
    ):
        action, amount, source = ActionType.RESHIP.value, "0", "规则：补发不涉及赔付"
    elif issue_type == IssueType.DAMAGED.value:
        action, amount, source = ActionType.RESHIP.value, "0", "规则：破损优先补发，赔付需凭证"
        missing.append("商品破损照片")
    elif issue_type == IssueType.LOGISTICS_STALLED.value:
        if stalled and int(stalled) >= 48:
            action = ActionType.CONTACT_LOGISTICS.value
            amount = parse_amount(policy_text)
            source = "政策条款（超时补偿标准）" if amount else None
            if not amount:
                missing.append("超时补偿标准（政策未命中或未写明金额）")
        else:
            action, amount, source = ActionType.CONTACT_LOGISTICS.value, "0", "规则：未达补偿门槛"
    elif issue_type == IssueType.PROMOTION_NOT_APPLIED.value:
        action, amount, source = ActionType.REPLY_ONLY.value, "0", "规则：优惠问题先解释口径"
    else:
        action, amount, source = ActionType.ASK_CUSTOMER.value, None, None
        missing.append("问题类型（未识别）")

    return {
        "action_type": action,
        "amount": amount,
        "amount_source": source,
        "rationale": _rationale(issue_type, facts.get("order_status"), facts.get("signed_at"), stalled, source),
        "missing_information": missing,
        "issue_type": issue_type,
        "conflict": conflict,
    }


class TriageOutput(AgentOutputModel):
    issue_type: str = IssueType.OTHER.value
    urgency: str = "medium"
    sentiment: str = Sentiment.NEUTRAL.value
    missing_fields: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class FactItem(BaseModel):
    source: str
    observed_at: str | None = None
    request_id: str | None = None
    is_mock: bool = True
    payload: dict = Field(default_factory=dict)
    error: dict | None = None


class OrderFactOutput(AgentOutputModel):
    facts: list[FactItem] = Field(default_factory=list)
    order_status: str | None = None
    paid_amount: str | None = None
    stalled_hours: int | None = None
    signed_at: str | None = None


class PolicyRef(BaseModel):
    title: str = ""
    version: str | None = None
    clause: str | None = None
    snippet: str = ""
    effective_from: str | None = None
    evidence_ref: str | None = None


class PolicyRagOutput(AgentOutputModel):
    policies: list[PolicyRef] = Field(default_factory=list)
    search_scope: str | None = None
    matched: int = 0


class ResolutionOutput(AgentOutputModel):
    action_type: str = ActionType.ASK_CUSTOMER.value
    amount: str | None = None
    amount_source: str | None = None
    rationale: str = ""
    missing_information: list[str] = Field(default_factory=list)
    policy_refs: list[str] = Field(default_factory=list)


class ResponseOutput(AgentOutputModel):
    customer_reply: str = ""
    internal_note: str = ""
    tone: str = "friendly"


class ReviewOutput(AgentOutputModel):
    risk_tier: str = "L2"
    risk_reason: str = ""
    approval_requirement: str = "staff_confirm"
    evidence_sufficient: bool = True
    amount_consistent: bool = True
    policy_conflict: bool = False
    escalate: bool = False
    warnings: list[str] = Field(default_factory=list)
    verdict: str = "pass"
    issues: list[ReflectionIssue] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    stop_reason: str | None = None


class RevisionOutput(AgentOutputModel):
    revised_text: str = ""
    addressed_issue_codes: list[str] = Field(default_factory=list)
    remaining_issue_codes: list[str] = Field(default_factory=list)
    changed_sections: list[str] = Field(default_factory=list)


class FollowUpOutput(AgentOutputModel):
    required: bool = False
    next_check_hours: int = 24
    reminder: str = ""
    escalate_after_hours: int | None = None


# --------------------------------------------------------------------------- 工具函数
def classify_issue(text: str) -> str:
    """归类问题类型：先按**优先级**（现象 > 诉求）取最高优先级的命中类型，同级再比出现位置。"""
    lowered = text or ""
    # 促销类写法（"满 199 减 30"）关键词拆不开，先按正则命中
    if any(pattern.search(lowered) for pattern in PROMOTION_PATTERNS):
        return IssueType.PROMOTION_NOT_APPLIED.value
    candidates: list[tuple[int, int, str]] = []
    for issue_type, keywords in ISSUE_KEYWORDS:
        for keyword in keywords:
            index = lowered.find(keyword)
            if index >= 0:
                candidates.append((ISSUE_PRIORITY.get(issue_type, 99), index, issue_type))
    if not candidates:
        return IssueType.OTHER.value
    candidates.sort()
    return candidates[0][2]


def wants_refund_explicitly(text: str) -> bool:
    """用户是否明确要退款（方案生成据此把"退货/破损"等诉求落到退款动作上）。"""
    lowered = text or ""
    return any(word in lowered for word in EXPLICIT_REFUND_WORDS)


def detect_sentiment(text: str) -> str:
    lowered = text or ""
    if any(keyword in lowered for keyword in ANGRY_KEYWORDS):
        return Sentiment.ANGRY.value
    return Sentiment.CALM.value


def detect_urgency(text: str) -> str:
    lowered = text or ""
    if any(keyword in lowered for keyword in URGENT_KEYWORDS):
        return "urgent"
    return "high" if any(k in lowered for k in HIGH_RISK_KEYWORDS) else "medium"


def parse_amount(text: str | None) -> str | None:
    """从政策文本里提取"补偿/赔付标准"的金额。**只提取，不计算。**

    坑（实测踩过）：政策原文"补偿标准：停滞超过 48 小时，……单笔不超过 ¥30"，
    朴素的"关键字后面第一个数字"会把 **48（小时）** 当成补偿金额。
    所以优先匹配**带货币符号或"元"**的金额，最后才退化为裸数字并排除小时/天/次/个/% 这类量词。
    """
    if not text:
        return None
    money_words = r"(?:补偿|赔付|补贴|不超过|最高|上限)"
    for pattern in (
        rf"{money_words}[^。；\n]{{0,24}}?(?:¥|￥)\s*(\d+(?:\.\d{{1,2}})?)",
        rf"{money_words}[^。；\n]{{0,24}}?(\d+(?:\.\d{{1,2}})?)\s*元",
        r"(?:¥|￥)\s*(\d+(?:\.\d{1,2})?)",
        r"(\d+(?:\.\d{1,2})?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    match = re.search(
        rf"{money_words}[^0-9]{{0,12}}(\d+(?:\.\d{{1,2}})?)(?!\s*(?:小时|天|次|个|%|分钟))",
        text,
    )
    return match.group(1) if match else None


def _iso(value) -> str | None:
    """把 datetime/str/None 统一成 ISO 字符串（Agent 输出 Schema 要求字符串）。"""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# --------------------------------------------------------------------------- 1. Triage
class TriageAgent(BaseAgent):
    code = "triage_agent"
    display_name = "问题分类 Agent"
    kb_scope: set[str] = set()
    output_model = TriageOutput

    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        return Collected()  # 分类只看工单文本，不需要检索

    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        text = ai.query or ""
        facts = ai.filters.get("fact_digest") or {}
        issue_type = classify_issue(text)
        missing: list[str] = []
        if not ai.filters.get("order_id"):
            missing.append("订单号")
        if issue_type == IssueType.DAMAGED.value and not ai.filters.get("attachments"):
            missing.append("商品照片")
        if issue_type == IssueType.MISSING_ITEM.value and not facts.get("package_weight"):
            missing.append("包裹重量照片")
        findings = [
            {
                "text": f"诉求归类为「{issue_type}」，紧急度 {detect_urgency(text)}，"
                f"情绪 {detect_sentiment(text)}。",
                "type": "fact",
                "evidence_refs": [],
                "confidence": 0.82 if issue_type != IssueType.OTHER.value else 0.4,
            }
        ]
        return {
            "summary": f"问题分类：{issue_type}" + (f"，缺 {len(missing)} 项信息" if missing else ""),
            "findings": findings,
            "issue_type": issue_type,
            "urgency": detect_urgency(text),
            "sentiment": detect_sentiment(text),
            "missing_fields": missing,
            "tags": [issue_type, detect_sentiment(text)],
            "data_gaps": [f"缺少{field}" for field in missing],
        }


# --------------------------------------------------------------------------- 2. Order Fact
class OrderFactAgent(BaseAgent):
    code = "order_fact_agent"
    display_name = "订单查询 Agent"
    kb_scope: set[str] = set()
    output_model = OrderFactOutput

    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        """只读查询订单/支付/物流/售后事实。工具层会记录 request_id 并拒绝任何写操作。"""
        collected = Collected()
        order_id = ai.filters.get("order_id")
        try:
            data = self.call_tool(
                ctx,
                "order.facts",
                {"order_id": order_id, "customer_id": ai.filters.get("customer_id")},
            )
        except Exception as exc:  # noqa: BLE001
            collected.tool_errors.append(f"订单事实查询失败: {exc}")
            collected.data_gaps.append("订单事实查询失败，需人工核对")
            return collected
        collected.blocks["order_facts"] = data
        return collected

    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        data = collected.blocks.get("order_facts") or {}
        raw_facts = data.get("facts") or []
        facts = [
            {
                "source": item.get("source"),
                # 适配器给的是 datetime，Schema 字段是 str：统一转 ISO（否则 pydantic 校验直接失败，
                # 实测踩过：facts.*.observed_at 报 "Input should be a valid string"）
                "observed_at": _iso(item.get("observed_at")),
                "request_id": item.get("request_id"),
                "is_mock": item.get("is_mock", True),
                "payload": item.get("payload") or {},
                "error": item.get("error"),
            }
            for item in raw_facts
        ]
        order_payload = next((f["payload"] for f in facts if f["source"] == "order"), {})
        pay_payload = next((f["payload"] for f in facts if f["source"] == "payment"), {})
        log_payload = next((f["payload"] for f in facts if f["source"] == "logistics"), {})
        stalled = log_payload.get("stalled_hours")
        findings = []
        if order_payload:
            findings.append(
                {
                    "text": f"订单事实：状态 {order_payload.get('order_status')}，"
                    f"金额 {order_payload.get('order_amount')}，商品 {order_payload.get('order_items')}。",
                    "type": "fact",
                    "evidence_refs": [],
                    "confidence": 0.95,
                }
            )
        if log_payload:
            findings.append(
                {
                    "text": f"物流事实：{log_payload.get('carrier')} {log_payload.get('latest_track')}"
                    + (f"，已停滞 {stalled} 小时" if stalled and stalled != "0" else ""),
                    "type": "fact",
                    "evidence_refs": [],
                    "confidence": 0.93,
                }
            )
        gaps = [
            f"{item.get('source')} 事实缺失：{(item.get('error') or {}).get('message')}"
            for item in raw_facts
            if item.get("error")
        ]
        return {
            "summary": f"已核验 {len([f for f in facts if not f['error']])}/{len(facts)} 类事实"
            + ("（模拟数据）" if facts and all(f["is_mock"] for f in facts) else ""),
            "findings": findings,
            "facts": facts,
            "order_status": order_payload.get("order_status"),
            "paid_amount": pay_payload.get("paid_amount"),
            "stalled_hours": int(stalled) if str(stalled or "").isdigit() else None,
            "signed_at": log_payload.get("signed_at") or None,
            "data_gaps": gaps,
        }


# --------------------------------------------------------------------------- 3. Policy RAG
def _policy_query(issue_type: str, base: str) -> str:
    """把问题类型的中文主题词拼进检索查询。

    只用 `damaged 处理规则 补偿标准` 这种英文类型名，向量召回会被"处理规则/补偿标准"
    这类通用词带偏；政策库变大后，真正含"破损/补发"的条款会被挤出 top-k。
    """
    from app.services.ticket_policy_relevance import POLICY_TOPIC_TERMS

    terms = POLICY_TOPIC_TERMS.get(str(issue_type or ""), ())
    return f"{issue_type} {' '.join(terms[:4])} {base}".strip()


class PolicyRagAgent(BaseAgent):
    code = "policy_rag_agent"
    display_name = "政策检索 Agent"
    kb_scope = {KnowledgeBaseCode.POLICY.value}
    output_model = PolicyRagOutput

    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        collected = Collected()
        issue_type = ai.filters.get("issue_type") or classify_issue(ai.query or "")
        queries = [
            _policy_query(issue_type, "处理规则 补偿标准"),
            "售后政策 处理规范 时效 适用范围",
        ]
        seen: set[str] = set()
        raw_hits: list[dict] = []
        for query in queries:
            try:
                data = self.call_tool(
                    ctx,
                    "retrieval.search",
                    {
                        "query": query,
                        "knowledge_bases": sorted(self.kb_scope & ai.kb_codes),
                        # 政策库变大后 5 条容易被通用词挤占；先多召回，再按主题词过滤。
                        "top_k": 8,
                        # 政策适用范围过滤要店铺与平台：不传会被保守判为"不适用"（宁可不用，不可用错）
                        "shop_id": ai.filters.get("shop_id"),
                        "platform": ai.filters.get("platform"),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                collected.tool_errors.append(f"政策检索失败: {exc}")
                continue
            for hit in data.get("hits") or []:
                key = hit.get("chunk_id")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                raw_hits.append(hit)
            self._check_deadline(deadline)

        raw_hits.sort(key=lambda h: h.get("relevance_score") or 0, reverse=True)
        from app.services.ticket_policy_relevance import filter_policy_evidence

        raw_hits = filter_policy_evidence(issue_type, raw_hits)
        collected.evidence = make_evidence(
            raw_hits[: get_settings().retrieval_top_k], agent_code=self.code
        )
        if not collected.evidence:
            collected.data_gaps.append("未检索到与当前问题类型匹配的售后政策条款")
        return collected

    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        policies: list[dict] = []
        for item in collected.evidence:
            snippet = (item.get("content") or "").strip().replace("\n", " ")
            title = item.get("source_name") or "售后政策"
            version = None
            clause = item.get("section_title")
            match = re.search(r"v\d+(?:\.\d+)*", f"{title} {snippet}")
            if match:
                version = match.group(0)
            policies.append(
                {
                    "title": title,
                    "version": version,
                    "clause": clause,
                    "snippet": snippet[:220],
                    "effective_from": item.get("effective_from"),
                    "evidence_ref": item.get("local_ref"),
                }
            )
        findings = []
        if policies:
            findings.append(
                {
                    "text": f"匹配 {len(policies)} 条有效政策条款，最高相关度条款来自《{policies[0]['title']}》"
                    f"{'（' + str(policies[0]['clause']) + '）' if policies[0].get('clause') else ''}。",
                    "type": "fact",
                    "evidence_refs": [p["evidence_ref"] for p in policies if p.get("evidence_ref")],
                    "confidence": 0.8,
                }
            )
        return {
            "summary": f"政策检索命中 {len(policies)} 条" if policies else "未命中适用政策",
            "findings": findings,
            "policies": policies,
            "search_scope": "、".join(sorted(self.kb_scope & ai.kb_codes)) or "无可见政策库",
            "matched": len(policies),
            "data_gaps": list(collected.data_gaps),
        }


# --------------------------------------------------------------------------- 4. Resolution
class ResolutionAgent(BaseAgent):
    code = "resolution_agent"
    display_name = "方案生成 Agent"
    kb_scope = {KnowledgeBaseCode.POLICY.value}
    output_model = ResolutionOutput

    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        collected = Collected()
        issue_type = ai.filters.get("issue_type") or IssueType.OTHER.value
        try:
            data = self.call_tool(
                ctx,
                "retrieval.search",
                {
                    "query": _policy_query(issue_type, "处理动作 补偿标准 责任判定"),
                    "knowledge_bases": sorted(self.kb_scope & ai.kb_codes),
                    "top_k": 8,
                    "shop_id": ai.filters.get("shop_id"),
                    "platform": ai.filters.get("platform"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            collected.tool_errors.append(f"方案检索失败: {exc}")
            return collected
        from app.services.ticket_policy_relevance import filter_policy_evidence

        relevant_hits = filter_policy_evidence(issue_type, data.get("hits") or [])
        collected.evidence = make_evidence(
            relevant_hits[: get_settings().retrieval_top_k], agent_code=self.code
        )
        if not collected.evidence:
            collected.data_gaps.append("未检索到与当前问题类型匹配的售后政策条款")
        return collected

    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        """按"事实 + 政策"给出动作。**受控字段全来自 `decide_action` 规则**（模型只能润色文字）。"""
        facts = ai.filters.get("fact_digest") or {}
        policy_text = " ".join((item.get("content") or "") for item in collected.evidence)
        issue_type = ai.filters.get("issue_type") or classify_issue(ai.query)
        decision = decide_action(
            query=ai.query, issue_type=issue_type, facts=facts, policy_text=policy_text
        )
        policy_refs = [item.get("local_ref") for item in collected.evidence if item.get("local_ref")]
        findings = [
            {
                "text": f"建议动作「{decision['action_type']}」"
                + (
                    f"，金额 {decision['amount']}（来源：{decision['amount_source']}）"
                    if decision["amount"] not in (None, "0")
                    else ""
                )
                + "。",
                "type": "recommendation",
                "evidence_refs": policy_refs[:3],
                "confidence": 0.75 if not decision["missing_information"] else 0.5,
            }
        ]
        return {
            "summary": f"处理方案：{decision['action_type']}"
            + (f"，参考 {len(policy_refs)} 条政策" if policy_refs else ""),
            "findings": findings,
            "data_gaps": [f"缺少{field}" for field in decision["missing_information"]],
            **decision,
        }


def _rationale(
    issue_type: str, order_status: str | None, signed: str | None, stalled: int, source: str | None
) -> str:
    parts = [f"问题类型为 {issue_type}"]
    if order_status:
        parts.append(f"订单状态 {order_status}")
    if signed:
        parts.append(f"物流签收时间 {signed}")
    if stalled:
        parts.append(f"物流停滞 {stalled} 小时")
    parts.append(f"结论依据：{source or '未取得可引用依据'}")
    return "；".join(parts) + "。"


# --------------------------------------------------------------------------- 5. Response
class ResponseAgent(BaseAgent):
    code = "response_agent"
    display_name = "回复草稿 Agent"
    kb_scope = {KnowledgeBaseCode.POLICY.value}
    output_model = ResponseOutput

    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        return Collected()  # 草稿基于上游结论，不额外检索（避免"自己找依据自己写话"）

    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        facts = ai.filters.get("fact_digest") or {}
        resolution = {
            **(ai.filters.get("resolution") or {}),
            "issue_type": ai.filters.get("issue_type"),
        }
        action = resolution.get("action_type") or ActionType.ASK_CUSTOMER.value
        order_id = ai.filters.get("order_id") or "您的订单"
        reply = _reply_template(action, order_id, facts, resolution)
        internal = (
            f"内部备注：动作={action}；风险={resolution.get('risk_tier') or '待评估'}；"
            f"依据={resolution.get('amount_source') or '—'}。"
        )
        return {
            "summary": "已生成回复草稿（消费者可见）与内部备注（不可见）",
            "findings": [
                {
                    "text": f"回复草稿面向消费者，动作口径为「{action}」；内部备注记录依据与风险，不对外发送。",
                    "type": "inference",
                    "evidence_refs": [],
                    "confidence": 0.7,
                }
            ],
            "customer_reply": reply,
            "internal_note": internal,
            "tone": "friendly" if ai.filters.get("sentiment") != Sentiment.ANGRY.value else "empathetic",
        }


def _reply_template(action: str, order_id: str, facts: dict, resolution: dict) -> str:
    amount = resolution.get("amount")
    head = "您好，非常抱歉给您带来不便。"
    if action == ActionType.REPLY_ONLY.value and resolution.get("issue_type") == IssueType.PROMOTION_NOT_APPLIED.value:
        eligible_amount = str(facts.get("eligible_amount") or "").strip()
        excluded_items = re.sub(r"\s+", " ", str(facts.get("excluded_items") or "")).strip()[:80]
        threshold = str(facts.get("promotion_threshold") or "").strip()
        valid_amount = re.fullmatch(r"\d+(?:\.\d{1,2})?", eligible_amount)
        valid_threshold = re.fullmatch(r"满\s*\d+(?:\.\d+)?\s*减\s*\d+(?:\.\d+)?", threshold)
        if valid_amount and excluded_items and valid_threshold:
            return (
                f"{head}经核对，订单中的非活动商品为{excluded_items}，扣除后符合条件的金额为 "
                f"{eligible_amount} 元，未达到活动门槛“{threshold}”，所以本次优惠未生效。"
                "您可以调整参与活动的商品后重新结算。"
            )
        return (
            f"{head}目前缺少这笔订单的活动适用明细，暂时无法确认优惠未生效的具体原因。"
            "请提供活动页面或结算页截图（包含商品、优惠券和金额），客服核对后给您明确答复。"
        )
    if action == ActionType.REFUND.value:
        return (
            f"{head}我们已核实订单 {order_id} 的支付与物流信息，将为您发起退款"
            + (f"（金额 {amount} 元）" if amount and amount != "0" else "")
            + "，预计 1-3 个工作日到账。"
        )
    if action == ActionType.RESHIP.value:
        return f"{head}已确认该订单存在发货问题，我们将为您免费补发，预计 48 小时内发出，请留意物流通知。"
    if action == ActionType.RETURN_GOODS.value:
        return f"{head}已为您登记退货，请在 7 天内寄回商品（保持商品与包装完整），收到后我们会尽快为您处理退款。"
    if action == ActionType.EXCHANGE.value:
        return f"{head}已为您登记换货，我们会安排寄出替换商品，请保留原商品包装以便退回。"
    if action == ActionType.CONTACT_LOGISTICS.value:
        stalled = facts.get("stalled_hours")
        return (
            f"{head}我们已联系承运商核实物流"
            + (f"（当前已停滞约 {stalled} 小时）" if stalled else "")
            + "，会持续跟进并在有进展时第一时间同步您。"
        )
    if action == ActionType.ASK_CUSTOMER.value:
        missing = "、".join(resolution.get("missing_information") or []) or "订单号"
        return f"{head}为了帮您快速处理，麻烦提供{missing}，我们会立即核实并回复。"
    return f"{head}我们已收到您的反馈，正在核实相关规则，稍后给您明确答复。"


# --------------------------------------------------------------------------- 6. Review
class ReviewAgent(BaseAgent):
    code = "review_agent"
    display_name = "风险与质检 Agent"
    kb_scope: set[str] = set()
    output_model = ReviewOutput

    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        return Collected()

    def tool_budget(self, ai: AgentInput) -> int:
        return 0

    def user_prompt(self, ai: AgentInput, collected: Collected) -> str:
        import json

        return (
            f"<客户原问题>{ai.query}</客户原问题>\n"
            f"<待复核回复>{ai.filters.get('draft_reply') or ''}</待复核回复>\n"
            f"<受保护决策>{json.dumps(ai.filters.get('decision') or {}, ensure_ascii=False, default=str)}</受保护决策>\n"
            f"<审批状态>{ai.filters.get('approval_status') or 'not_required'}</审批状态>\n"
            "只输出结构化问题与 verdict，不得建议修改动作、金额、风险等级、审批要求或政策引用。"
        )

    def _validate_output(self, payload: dict) -> tuple[dict, list[str]]:
        """模型常把 issues 写成 issue_type/description/evidence，先做保守归一。

        2026-11-08 真实模型试点暴露：10 单里 4 单 review_agent 因为 issues 字段名
        不符合 `ReflectionIssue`（code/category/target/source/message）被整段丢弃，
        质检退化成确定性兜底。这里只做字段别名与枚举收敛，不改变模型结论；
        仍按强类型 Schema 校验，非法内容不会被放行。
        """
        normalized = dict(payload or {})
        raw_issues = normalized.get("issues")
        if isinstance(raw_issues, list):
            categories = {
                "evidence",
                "policy",
                "amount",
                "action",
                "approval",
                "citation",
                "consistency",
                "reply",
                "sensitive_data",
            }
            severities = {"low", "medium", "high", "critical"}
            targets = {"ticket_reply", "diagnostic_report"}
            cleaned: list[dict] = []
            for item in raw_issues:
                if not isinstance(item, dict):
                    continue
                alias = str(item.get("issue_type") or item.get("type") or "").strip().lower()
                category = str(item.get("category") or alias or "reply").strip().lower()
                if category not in categories:
                    category = "reply"
                severity = str(item.get("severity") or "medium").strip().lower()
                if severity not in severities:
                    severity = "medium"
                target = str(item.get("target") or "ticket_reply").strip().lower()
                if target not in targets:
                    target = "ticket_reply"
                source = str(item.get("source") or "model").strip().lower()
                if source not in {"code", "model"}:
                    source = "model"
                rule_code = item.get("rule_code")
                if source == "code" and not rule_code:
                    # 代码门禁问题必须带 rule_code；模型给不出来就按模型问题处理。
                    source = "model"
                refs = item.get("evidence_refs")
                if not isinstance(refs, list):
                    refs = item.get("evidence") or []
                if not isinstance(refs, list):
                    refs = [refs]
                message = str(
                    item.get("message")
                    or item.get("description")
                    or item.get("detail")
                    or "模型质检问题"
                )[:800]
                cleaned.append(
                    {
                        "code": str(item.get("code") or alias or "model_issue")[:80],
                        "severity": severity,
                        "category": category,
                        "target": target,
                        "source": source,
                        "rule_code": str(rule_code)[:80] if rule_code else None,
                        "message": message,
                        "evidence_refs": [str(ref)[:200] for ref in refs][:20],
                        "auto_fixable": bool(item.get("auto_fixable", False)),
                    }
                )
            normalized["issues"] = cleaned

        verdict = str(normalized.get("verdict") or "").strip().lower()
        verdict_aliases = {
            "ok": "pass",
            "approved": "pass",
            "fail": "human_review",
            "reject": "human_review",
            "needs_review": "human_review",
        }
        verdict = verdict_aliases.get(verdict, verdict)
        if verdict not in {"pass", "revise", "retrieve_more", "human_review"}:
            verdict = "human_review" if normalized.get("issues") else "pass"
        issues = normalized.get("issues") or []
        if verdict == "pass" and any(
            item.get("severity") in {"high", "critical"} for item in issues
        ):
            verdict = "human_review"
        if verdict == "revise" and not any(item.get("auto_fixable") for item in issues):
            verdict = "human_review"
        normalized["verdict"] = verdict
        # Model providers occasionally pair human_review with stop_reason=passed.
        # Keep the useful issue list, but do not persist a self-contradictory
        # execution reason in the trace or business view.
        if verdict != "pass" and normalized.get("stop_reason") == "passed":
            normalized["stop_reason"] = None
        elif verdict == "pass" and not normalized.get("stop_reason"):
            normalized["stop_reason"] = "passed"
        normalized.setdefault("confidence", 0.6)
        return super()._validate_output(normalized)

    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        from app.services.ticket_risk import assess

        facts = ai.filters.get("fact_digest") or {}
        resolution = ai.filters.get("resolution") or {}
        policies = ai.filters.get("policy_count") or 0
        action = resolution.get("action_type") or ActionType.ASK_CUSTOMER.value
        amount = resolution.get("amount")
        warnings: list[str] = []

        # 政策缺失或未命中 → 不允许直接下结论（需求书：任何证据不足进人工）
        evidence_sufficient = policies > 0 and not (resolution.get("missing_information") or [])
        if policies == 0:
            warnings.append("未命中适用政策，方案缺少政策依据")
        if resolution.get("missing_information"):
            warnings.append("缺少必要信息：" + "、".join(resolution["missing_information"]))
        if not facts.get("order_status"):
            warnings.append("未取得订单事实")

        # 金额一致性：退款金额不得超过实付金额
        paid = _to_float(facts.get("paid_amount"))
        claim = _to_float(amount)
        amount_consistent = True
        if action == ActionType.REFUND.value and paid is not None and claim is not None:
            if claim > paid:
                amount_consistent = False
                warnings.append(f"退款金额 {claim} 超过实付金额 {paid}")

        risk = assess(
            action_type=action,
            amount=claim or 0,
            flags=list(ai.filters.get("risk_flags") or []),
            conflict=bool(ai.filters.get("conflict")),
            sensitive_customer=bool(ai.filters.get("sensitive_customer")),
        )
        if not evidence_sufficient and risk.tier in ("L1", "L2"):
            # 证据不足时至少升到 L2（客服确认），不能静默自动出结论
            risk = assess(
                action_type=action, amount=claim or 0, conflict=True, settings=None
            )
            warnings.append("证据不足，风险等级上调至人工确认")
        if not amount_consistent:
            warnings.append("金额一致性校验未通过，需人工复核")

        escalate = risk.tier == "L4" or not amount_consistent
        findings = [
            {
                "text": f"质检结论：风险等级 {risk.tier}（{risk.reason}），"
                f"证据{'充足' if evidence_sufficient else '不足'}，金额一致性{'通过' if amount_consistent else '未通过'}。",
                "type": "inference",
                "evidence_refs": [],
                "confidence": 0.68,
            }
        ]
        reflection = None
        if ai.filters.get("reflection_enabled") and ai.filters.get("draft_reply") is not None:
            from app.services.reflection_guard import inspect_ticket_reply

            reflection = inspect_ticket_reply(
                reply=str(ai.filters.get("draft_reply") or ""),
                protected_decision=ai.filters.get("decision") or {},
                approval_status=ai.filters.get("approval_status"),
                evidence_refs=set(ai.filters.get("reflection_evidence_refs") or []),
            )

        return {
            "summary": f"质检：{risk.tier}，{'需审批' if risk.approval_requirement != 'none' else '可自动出草稿'}",
            "findings": findings,
            "risk_tier": risk.tier,
            "risk_reason": risk.reason,
            "approval_requirement": risk.approval_requirement,
            "evidence_sufficient": evidence_sufficient,
            "amount_consistent": amount_consistent,
            "policy_conflict": bool(ai.filters.get("conflict")),
            "escalate": escalate,
            "warnings": warnings,
            "data_gaps": warnings,
            "verdict": reflection.verdict if reflection else "pass",
            "issues": [item.model_dump() for item in reflection.issues] if reflection else [],
            "required_changes": reflection.required_changes if reflection else [],
            "missing_evidence": reflection.missing_evidence if reflection else [],
            "stop_reason": reflection.stop_reason if reflection else None,
        }


class RevisionAgent(BaseAgent):
    """只修订客户回复表达层；工具预算为零，Schema 不含受保护字段。"""

    code = "revision_agent"
    display_name = "回复修订 Agent"
    kb_scope: set[str] = set()
    output_model = RevisionOutput

    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        return Collected()

    def tool_budget(self, ai: AgentInput) -> int:
        return 0

    def user_prompt(self, ai: AgentInput, collected: Collected) -> str:
        import json

        return (
            f"<原回复>{ai.filters.get('draft_reply') or ''}</原回复>\n"
            f"<必须修复的问题>{json.dumps(ai.filters.get('reflection_issues') or [], ensure_ascii=False)}</必须修复的问题>\n"
            "只返回 revised_text、addressed_issue_codes、remaining_issue_codes、changed_sections。"
            "不得返回或修改动作、金额、风险等级、审批要求、政策引用。"
        )

    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        from app.services.reflection_guard import revise_ticket_reply

        revision = revise_ticket_reply(
            reply=str(ai.filters.get("draft_reply") or ""),
            issues=ai.filters.get("reflection_issues") or [],
            protected_decision=ai.filters.get("decision") or {},
        )
        return {
            "summary": "已按结构化问题修订客户回复",
            "findings": [
                {
                    "text": "仅调整客户回复表达，受保护业务决策保持不变。",
                    "type": "inference",
                    "evidence_refs": [],
                    "confidence": 0.9,
                }
            ],
            **revision,
        }


def _to_float(value) -> float | None:
    try:
        return float(str(value).replace("元", "").strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- 7. Follow-up
class FollowUpAgent(BaseAgent):
    code = "followup_agent"
    display_name = "跟进 Agent"
    kb_scope: set[str] = set()
    output_model = FollowUpOutput

    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        return Collected()

    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        action = (ai.filters.get("resolution") or {}).get("action_type")
        risk_tier = (ai.filters.get("risk") or {}).get("risk_tier", "L1")
        waiting_external = action in (
            ActionType.CONTACT_LOGISTICS.value,
            ActionType.RESHIP.value,
            ActionType.RETURN_GOODS.value,
            ActionType.EXCHANGE.value,
        )
        if not waiting_external:
            return {
                "summary": "无需外部等待，不创建跟进提醒",
                "findings": [],
                "required": False,
                "next_check_hours": 0,
                "reminder": "",
                "escalate_after_hours": None,
            }
        hours = 24 if risk_tier in ("L1", "L2") else 4
        return {
            "summary": f"需跟进：{hours} 小时后检查处理进展，超时升级",
            "findings": [
                {
                    "text": f"该方案依赖外部处理（{action}），{hours} 小时后自动检查；"
                    f"超过 {hours * 2} 小时未闭环则升级主管。",
                    "type": "recommendation",
                    "evidence_refs": [],
                    "confidence": 0.6,
                }
            ],
            "required": True,
            "next_check_hours": hours,
            "reminder": f"跟进：工单仍在等待外部处理（动作 {action}），请检查是否已闭环",
            "escalate_after_hours": hours * 2,
        }


AFTER_SALES_AGENTS: tuple[str, ...] = (
    "triage_agent",
    "order_fact_agent",
    "policy_rag_agent",
    "resolution_agent",
    "response_agent",
    "review_agent",
    "followup_agent",
)

AFTER_SALES_AGENT_CLASSES: dict[str, type[BaseAgent]] = {
    "triage_agent": TriageAgent,
    "order_fact_agent": OrderFactAgent,
    "policy_rag_agent": PolicyRagAgent,
    "resolution_agent": ResolutionAgent,
    "response_agent": ResponseAgent,
    "review_agent": ReviewAgent,
    "revision_agent": RevisionAgent,
    "followup_agent": FollowUpAgent,
}

__all__ = [
    "AFTER_SALES_AGENT_CLASSES",
    "AFTER_SALES_AGENTS",
    "FollowUpAgent",
    "OrderFactAgent",
    "PolicyRagAgent",
    "ResolutionAgent",
    "ResponseAgent",
    "ReviewAgent",
    "RevisionAgent",
    "TriageAgent",
    "classify_issue",
    "detect_sentiment",
    "detect_urgency",
    "parse_amount",
]

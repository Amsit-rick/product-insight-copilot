"""策略引擎（PolicyEngine）。

规则决定什么能做——这是普通代码职责，不交给 Agent：
- Agent 能调用哪些工具、能访问哪些知识库（§7.3 Agent 边界）
- 提示词注入检测（§11）
- 风险分级与人工审核判定（§7.1 verify_evidence 分支）
- 输出敏感内容扫描
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import get_settings
from app.models.enums import AgentCode, KnowledgeBaseCode, RiskLevel

# Agent -> 允许使用的工具
AGENT_TOOLS: dict[str, set[str]] = {
    AgentCode.PRODUCT.value: {"retrieval.search"},
    AgentCode.FEEDBACK.value: {"retrieval.search"},
    AgentCode.RELEASE.value: {"retrieval.search"},
    # 指标 Agent 除了受控指标查询，还需要读运营知识库解释口径与实验重叠
    AgentCode.METRIC.value: {"metric.query", "retrieval.search"},
    AgentCode.REPORT.value: set(),  # 报告 Agent 不引入未检索事实，不给工具
    # ---- 售后工单 7 个 Agent（需求书 §3.2）----
    # 分类/质检/跟进只处理上游已核验的信息，不需要任何工具（也就没有越权可能）
    AgentCode.TRIAGE.value: set(),
    # 订单事实 Agent 只能调"只读事实查询"，工具层会拒绝任何写意图并记录 request_id
    AgentCode.ORDER_FACT.value: {"order.facts"},
    AgentCode.POLICY_RAG.value: {"retrieval.search"},
    AgentCode.RESOLUTION.value: {"retrieval.search"},
    AgentCode.RESPONSE.value: set(),  # 草稿只基于上游结论，避免"自己找依据自己写话"
    AgentCode.REVIEW.value: set(),
    AgentCode.FOLLOW_UP.value: set(),
}

# Agent -> 允许访问的知识库
AGENT_KBS: dict[str, set[str]] = {
    AgentCode.PRODUCT.value: {KnowledgeBaseCode.PRODUCT.value},
    AgentCode.FEEDBACK.value: {KnowledgeBaseCode.FEEDBACK.value},
    AgentCode.RELEASE.value: {KnowledgeBaseCode.ENGINEERING.value},
    # operation_kb（指标定义 / 实验记录 / 报表说明）归属于指标解读职责：
    # 没有这一条，运营知识库将没有任何 Agent 能访问，永远是"死库"。
    AgentCode.METRIC.value: {KnowledgeBaseCode.OPERATION.value},
    AgentCode.REPORT.value: set(),
    # ---- 售后工单 ----
    AgentCode.TRIAGE.value: set(),
    AgentCode.ORDER_FACT.value: set(),
    AgentCode.POLICY_RAG.value: {KnowledgeBaseCode.POLICY.value},
    AgentCode.RESOLUTION.value: {KnowledgeBaseCode.POLICY.value},
    AgentCode.RESPONSE.value: {KnowledgeBaseCode.POLICY.value},
    AgentCode.REVIEW.value: set(),
    AgentCode.FOLLOW_UP.value: set(),
}


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str
    requires_approval: bool = False


class PolicyEngine:
    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------ 工具调用管控
    def check_tool_call(self, agent_code: str, tool_name: str, arguments: dict) -> PolicyDecision:
        allowed_tools = AGENT_TOOLS.get(agent_code)
        if allowed_tools is None:
            return PolicyDecision(False, f"未知 Agent: {agent_code}")
        if tool_name not in allowed_tools:
            return PolicyDecision(False, f"Agent {agent_code} 无权调用工具 {tool_name}")

        if tool_name == "retrieval.search":
            requested = set(arguments.get("knowledge_bases") or [])
            permitted = AGENT_KBS.get(agent_code, set())
            if not requested:
                return PolicyDecision(True, "ok")
            if not requested.issubset(permitted):
                outside = sorted(requested - permitted)
                return PolicyDecision(False, f"Agent {agent_code} 越权访问知识库: {outside}")
        if tool_name == "metric.query":
            # 指标工具只接受受控能力，禁止 SQL
            if "sql" in arguments or "raw_sql" in arguments:
                return PolicyDecision(False, "禁止通过指标工具执行任意 SQL")
            if arguments.get("capability") not in {
                "get_trend",
                "compare_versions",
                "breakdown",
                "detect_anomaly",
            }:
                return PolicyDecision(False, "非法的指标能力调用")
        if tool_name == "order.facts":
            # 事实查询**只读**：任何写意图（退款/补发/改价）都必须被拒绝，
            # 这是需求书 §1.2「不自动执行无审批的退款、赔付、改价或封禁」的工具层兜底。
            forbidden = {"refund", "write", "update", "reverse", "reship", "compensate", "action"}
            if forbidden & set(arguments):
                return PolicyDecision(False, "事实适配器只读，禁止任何写操作")
            if arguments.get("order_id") in (None, ""):
                return PolicyDecision(False, "缺少订单号，无法查询订单事实")
        return PolicyDecision(True, "ok")

    # ------------------------------------------------------------ 提示词注入检测
    def detect_injection(self, text: str) -> str | None:
        lowered = (text or "").lower()
        for pattern in self.settings.injection_patterns:
            if pattern and pattern in lowered:
                return pattern
        # 常见越权/泄露/绕过安全规则意图的正则
        suspicious = [
            r"忽略\s*(以上|前面|之前)?\s*(所有)?\s*(指令|规则|约束)",
            r"(reveal|print|show)\s+(your\s+)?(system\s+)?prompt",
            r"(导出|dump)\s*(全部|所有)?\s*(用户|数据|文档)",
            # 角色扮演 / 运维模式类绕过
            r"(运维|开发者|调试|god|root|admin)\s*模式",
            r"(jailbreak|dan\s+mode|developer\s+mode|bypass\s+(the\s+)?(safety|filter|guard))",
            # 直接要求关闭平台自身的受控机制
            r"(关闭|跳过|绕过|禁用|不要)\s*(引用校验|来源标注|密级|权限|审核|安全|过滤|鉴权)",
            r"(跳过|绕过|无视)\s*(密级|权限|审核|租户)\s*(过滤|校验|检查)?",
            r"(不要|无需|别)\s*(标注|给出)\s*(来源|引用|出处)",
            r"(top_?k|阈值|限额)\s*(调|改|设)\s*(成|为)?\s*\d+",
        ]
        for pattern in suspicious:
            if re.search(pattern, lowered):
                return pattern
        return None

    # ------------------------------------------------------------ 输出安全检查
    def scan_output(self, text: str) -> str | None:
        lowered = (text or "").lower()
        for pattern in self.settings.sensitive_patterns:
            if pattern and pattern in lowered:
                return pattern
        return None

    # ------------------------------------------------------------ 风险与人工审核
    def assess_risk(
        self,
        *,
        confidence: float | None,
        evidence_count: int,
        conflict_count: int,
        injection_hit: bool,
        metric_available: bool,
        priority: str | None = None,
    ) -> tuple[str, bool, list[str]]:
        """返回 (risk_level, needs_human_review, reasons)。"""
        reasons: list[str] = []
        risk = RiskLevel.LOW

        if injection_hit:
            risk = RiskLevel.HIGH
            reasons.append("检测到提示词注入特征")
        if conflict_count > 0:
            risk = RiskLevel.MEDIUM
            reasons.append(f"存在 {conflict_count} 处证据冲突")
        if evidence_count < self.settings.min_evidence_count:
            risk = RiskLevel.MEDIUM
            reasons.append(f"证据数量不足（{evidence_count} < {self.settings.min_evidence_count}）")
        if confidence is not None and confidence < self.settings.confidence_low_threshold:
            risk = RiskLevel.MEDIUM
            reasons.append(f"置信度偏低（{confidence:.2f}）")
        if not metric_available:
            reasons.append("缺少指标数据支撑")

        score = 0.0
        if injection_hit:
            score += 0.5
        score += min(conflict_count, 3) * 0.1
        if evidence_count < self.settings.min_evidence_count:
            score += 0.2
        if confidence is not None:
            score += max(0.0, self.settings.confidence_low_threshold - confidence)
        if priority in {"P0", "P1"}:
            score += 0.1
        if score >= self.settings.risk_score_high_threshold:
            risk = RiskLevel.HIGH

        needs_review = risk == RiskLevel.HIGH or injection_hit or conflict_count > 0
        if needs_review:
            reasons.append("高风险或存在冲突，进入人工审核")
        return risk.value, needs_review, reasons


_policy_engine: PolicyEngine | None = None


def get_policy_engine() -> PolicyEngine:
    global _policy_engine
    if _policy_engine is None:
        _policy_engine = PolicyEngine()
    return _policy_engine

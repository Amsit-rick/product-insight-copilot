"""Agent 输入输出契约（设计文档 7.4 节：每个节点定义输入与输出 Schema）。

约束：
- Agent 输出只是**候选事实**，必须带 evidence_refs，最终由证据合并与校验环节裁决；
- 所有输出都能 JSON 序列化，便于落 `agent_runs.output` 与断点恢复；
- 报告 Agent 不允许引入未检索事实，因此它的输入只能是结构化中间结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.orm import Session

from app.models.enums import StatementType

StatementKind = Literal["fact", "inference", "hypothesis", "recommendation"]


@dataclass
class AgentInput:
    """一次 Agent 运行的输入切片（只给它职责所需的上下文）。"""

    session: Session
    task_id: str
    tenant_id: str
    user_id: str
    query: str
    filters: dict = field(default_factory=dict)
    intent: str | None = None
    plan_item: dict = field(default_factory=dict)
    # 当前用户可见的知识库 ∩ 该 Agent 的职责知识库
    kb_codes: set[str] = field(default_factory=set)
    max_security_rank: int = 1
    # 发起人的角色码（由入口节点解析后随状态传下来，供指标级密级判定）
    roles: list[str] = field(default_factory=list)

    @property
    def app_version(self) -> str | None:
        return self.filters.get("app_version")

    @property
    def platform(self) -> str | None:
        return self.filters.get("platform")

    @property
    def date_from(self):
        return self.filters.get("date_from")

    @property
    def date_to(self):
        return self.filters.get("date_to")

    def sub_query(self, *extra: str) -> str:
        """在原始问题后追加职责关键词，形成该 Agent 的检索问题。"""
        parts = [self.query, *[e for e in extra if e]]
        return " ".join(parts)


class FindingCandidate:
    """轻量候选陈述（内部构造，最终统一转成 dict 落 state）。"""

    __slots__ = ("statement_id", "kind", "text", "evidence_refs", "confidence", "tags")

    def __init__(
        self,
        statement_id: str,
        kind: StatementKind,
        text: str,
        evidence_refs: list[str] | None = None,
        confidence: float = 0.6,
        tags: list[str] | None = None,
    ) -> None:
        self.statement_id = statement_id
        self.kind = kind
        self.text = text
        self.evidence_refs = evidence_refs or []
        self.confidence = confidence
        self.tags = tags or []

    def as_dict(self) -> dict:
        return {
            "id": self.statement_id,
            "type": self.kind,
            "text": self.text,
            "evidence_refs": list(self.evidence_refs),
            "confidence": round(float(self.confidence), 4),
            "tags": list(self.tags),
        }


@dataclass
class AgentResult:
    """Agent 运行结果（节点消费的唯一结构）。"""

    agent_code: str
    ok: bool
    summary: str = ""
    findings: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    metric_results: list[dict] = field(default_factory=list)
    blocks: dict = field(default_factory=dict)
    confidence: float = 0.0
    data_gaps: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    model_used: bool = False
    model_name: str | None = None
    # 模型调用失败的原因（未配置 / 超时 / 输出非法 JSON…），便于排查"为什么没用上模型"
    model_error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 命中供应商 prompt 缓存的输入 token（计费更便宜，单独记录便于成本核算）
    cache_hit_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1
    error: str | None = None
    schema_ok: bool = True
    schema_errors: list[str] = field(default_factory=list)
    agent_run_id: str | None = None

    def as_state(self) -> dict:
        """写入工作流 state 的精简版本（避免 state 过大）。"""
        return {
            "agent_code": self.agent_code,
            "ok": self.ok,
            "summary": self.summary,
            "findings": self.findings,
            "confidence": round(float(self.confidence), 4),
            "data_gaps": self.data_gaps,
            "blocks": self.blocks,
            "model_used": self.model_used,
            "model_name": self.model_name,
            "model_error": self.model_error,
            "tokens": self.prompt_tokens + self.completion_tokens,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "agent_run_id": self.agent_run_id,
            "error": self.error,
            "schema_ok": self.schema_ok,
            "schema_errors": self.schema_errors,
        }


def statement_type_of(kind: str) -> StatementType:
    return {
        "fact": StatementType.FACT,
        "inference": StatementType.INFERENCE,
        "hypothesis": StatementType.HYPOTHESIS,
        "recommendation": StatementType.RECOMMENDATION,
    }.get(kind, StatementType.FACT)

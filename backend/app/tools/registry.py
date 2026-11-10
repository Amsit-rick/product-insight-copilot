"""工具注册表：Agent 唯一的取数入口。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_utils import get_logger
from app.models.workflow import ToolCall
from app.services.policy_engine import PolicyEngine, get_policy_engine

logger = get_logger(__name__)


class ToolBudgetExceeded(Exception):
    """单次 Agent 运行的工具调用次数超限。"""


class ReplayPolicy(str, Enum):
    """工具在 checkpoint 恢复或任务重放时的执行语义。"""

    SAFE = "safe"
    IDEMPOTENT = "idempotent"
    UNSAFE = "unsafe"


class SideEffectLevel(str, Enum):
    """工具对外部世界造成的副作用级别。"""

    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


@dataclass
class ToolContext:
    """一次工具调用的执行上下文：把权限、范围、预算显式传下去。"""

    session: Session
    task_id: str
    tenant_id: str
    user_id: str
    agent_code: str
    max_security_rank: int = 1
    # 发起人的角色码：指标级密级判定要用（权限只从服务端会话解析后一路传下来，不接受外部传入）
    roles: list[str] = field(default_factory=list)
    # 当前用户可见且当前 Agent 可访问的知识库交集（由调用方先行求交）
    allowed_kb_codes: set[str] = field(default_factory=set)
    agent_run_id: str | None = None
    # 单次 Agent 运行内的调用预算，注册表负责递减
    budget: int | None = None
    calls: list[dict] = field(default_factory=list)
    # checkpoint 恢复后重新进入节点时必须显式标记，工具层据此阻止危险重放。
    replaying: bool = False
    # 人工审批结果由服务端恢复流程注入，不能由模型参数伪造。
    approved_tool_calls: set[str] = field(default_factory=set)
    approval_reference: str | None = None
    # 调用链可统一提供幂等键；工具参数中的 idempotency_key 优先。
    idempotency_key: str | None = None

    @property
    def used(self) -> int:
        return len(self.calls)

    def spend(self) -> None:
        limit = self.budget if self.budget is not None else get_settings().agent_max_tool_calls
        if self.used >= limit:
            raise ToolBudgetExceeded(f"工具调用次数超过上限 {limit}")


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., dict]
    replay_policy: ReplayPolicy
    side_effect_level: SideEffectLevel
    arg_schema: dict = field(default_factory=dict)
    idempotency_scope: str | None = None
    requires_approval: bool = False
    # 是否需要真实数据库会话（指标与检索都需要）
    requires_session: bool = True


@dataclass
class ToolOutcome:
    ok: bool
    tool_name: str
    data: dict = field(default_factory=dict)
    error: str | None = None
    policy_decision: str = "allow"
    denied_reason: str | None = None
    latency_ms: int = 0
    replay_policy: str | None = None
    side_effect_level: str | None = None
    idempotency_key: str | None = None
    replayed: bool = False
    approval_reference: str | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "tool_name": self.tool_name,
            "error": self.error,
            "policy_decision": self.policy_decision,
            "denied_reason": self.denied_reason,
            "latency_ms": self.latency_ms,
            "replay_policy": self.replay_policy,
            "side_effect_level": self.side_effect_level,
            "idempotency_key": self.idempotency_key,
            "replayed": self.replayed,
            "approval_reference": self.approval_reference,
        }


class ToolRegistry:
    """工具注册表 + 策略闸门 + 审计落库。"""

    def __init__(self, policy: PolicyEngine | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.policy = policy or get_policy_engine()
        self.settings = get_settings()

    # ------------------------------------------------------------------ 注册
    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册: {spec.name}")
        self._validate_contract(spec)
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "arg_schema": s.arg_schema,
                "replay_policy": s.replay_policy.value,
                "side_effect_level": s.side_effect_level.value,
                "idempotency_scope": s.idempotency_scope,
                "requires_approval": s.requires_approval,
            }
            for s in self._tools.values()
        ]

    def tools_for_agent(self, agent_code: str) -> list[str]:
        from app.services.policy_engine import AGENT_TOOLS

        allowed = AGENT_TOOLS.get(agent_code)
        if allowed is None:
            return []
        return sorted(allowed & set(self._tools))

    # ------------------------------------------------------------------ 调用
    def call(self, name: str, arguments: dict, ctx: ToolContext) -> ToolOutcome:
        started = time.perf_counter()
        spec = self._tools.get(name)
        if spec is None:
            return self._deny(ctx, name, f"未知工具: {name}", started, persist=False)

        # 闸门一：Agent 白名单
        decision = self.policy.check_tool_call(ctx.agent_code, name, arguments)
        if not decision.allowed:
            return self._deny(
                ctx,
                name,
                decision.reason,
                started,
                arguments=arguments,
                spec=spec,
            )

        # 闸门二：知识库范围（只能落在调用方给定的交集内）
        requested = set(arguments.get("knowledge_bases") or [])
        if requested:
            outside = sorted(requested - ctx.allowed_kb_codes)
            if outside:
                return self._deny(
                    ctx,
                    name,
                    f"越权访问知识库: {outside}",
                    started,
                    arguments=arguments,
                    spec=spec,
                )

        idempotency_key = str(arguments.get("idempotency_key") or ctx.idempotency_key or "").strip()
        if spec.replay_policy is ReplayPolicy.IDEMPOTENT and not idempotency_key:
            return self._deny(
                ctx,
                name,
                "幂等工具缺少稳定幂等键，已阻止执行",
                started,
                arguments=arguments,
                spec=spec,
            )
        approved = name in ctx.approved_tool_calls and bool(ctx.approval_reference)
        if ctx.replaying and spec.replay_policy is ReplayPolicy.UNSAFE:
            return self._deny(
                ctx,
                name,
                "不可安全重放的工具必须退出恢复上下文，由人工发起新的执行动作",
                started,
                arguments=arguments,
                spec=spec,
                policy_decision="require_approval",
            )
        if spec.requires_approval and not approved:
            return self._deny(
                ctx,
                name,
                "工具需要持久化人工审批后才能执行",
                started,
                arguments=arguments,
                spec=spec,
                policy_decision="require_approval",
            )
        # 闸门三：调用预算
        try:
            ctx.spend()
        except ToolBudgetExceeded as exc:
            return self._deny(
                ctx,
                name,
                str(exc),
                started,
                arguments=arguments,
                spec=spec,
            )

        outcome: ToolOutcome
        try:
            kwargs = dict(arguments)
            if spec.replay_policy is ReplayPolicy.IDEMPOTENT and idempotency_key:
                kwargs.setdefault("idempotency_key", idempotency_key)
            if spec.requires_session:
                data = spec.handler(session=ctx.session, ctx=ctx, **kwargs)
            else:
                data = spec.handler(ctx=ctx, **kwargs)
            outcome = ToolOutcome(
                ok=True,
                tool_name=name,
                data=data or {},
                policy_decision="allow",
                latency_ms=int((time.perf_counter() - started) * 1000),
                replay_policy=spec.replay_policy.value,
                side_effect_level=spec.side_effect_level.value,
                idempotency_key=idempotency_key or None,
                replayed=ctx.replaying,
                approval_reference=ctx.approval_reference if approved else None,
            )
        except Exception as exc:  # 工具异常不抛出，转为结构化失败，交给节点决定重试
            logger.warning("工具调用失败 %s: %s", name, exc)
            outcome = ToolOutcome(
                ok=False,
                tool_name=name,
                error=f"{type(exc).__name__}: {exc}",
                policy_decision="allow",
                latency_ms=int((time.perf_counter() - started) * 1000),
                replay_policy=spec.replay_policy.value,
                side_effect_level=spec.side_effect_level.value,
                idempotency_key=idempotency_key or None,
                replayed=ctx.replaying,
                approval_reference=ctx.approval_reference if approved else None,
            )

        ctx.calls.append(
            {
                "tool": name,
                "ok": outcome.ok,
                "args": _safe_args(arguments),
                "replay_policy": spec.replay_policy.value,
                "replayed": ctx.replaying,
            }
        )
        self._persist(ctx, name, arguments, outcome, spec=spec)
        return outcome

    # ------------------------------------------------------------------ 内部
    def _deny(
        self,
        ctx: ToolContext,
        name: str,
        reason: str,
        started: float,
        *,
        persist: bool = True,
        arguments: dict | None = None,
        spec: ToolSpec | None = None,
        policy_decision: str = "deny",
    ) -> ToolOutcome:
        outcome = ToolOutcome(
            ok=False,
            tool_name=name,
            error=reason,
            policy_decision=policy_decision,
            denied_reason=reason,
            latency_ms=int((time.perf_counter() - started) * 1000),
            replay_policy=spec.replay_policy.value if spec else None,
            side_effect_level=spec.side_effect_level.value if spec else None,
            idempotency_key=(
                str((arguments or {}).get("idempotency_key") or ctx.idempotency_key or "").strip()
                or None
            ),
            replayed=ctx.replaying,
            approval_reference=ctx.approval_reference,
        )
        ctx.calls.append(
            {
                "tool": name,
                "ok": False,
                "args": _safe_args(arguments or {}),
                "replay_policy": spec.replay_policy.value if spec else None,
                "replayed": ctx.replaying,
            }
        )
        logger.warning("工具调用被策略拒绝 agent=%s tool=%s reason=%s", ctx.agent_code, name, reason)
        if persist:
            self._persist(ctx, name, arguments or {}, outcome, spec=spec)
        return outcome

    def _persist(
        self,
        ctx: ToolContext,
        name: str,
        arguments: dict,
        outcome: ToolOutcome,
        *,
        spec: ToolSpec | None = None,
    ) -> None:
        """所有工具调用（含被拒绝的）都落审计表。

        走短写事务：工具调用发生在并行 Agent 内部，若挂在节点的长事务上，
        写锁会被拖到模型调用结束（SQLite 下直接互相锁死）。
        """
        from app.db.session import write_scope

        try:
            from app.services.task_replay import canonical_digest, sanitize_snapshot

            safe_result = sanitize_snapshot(outcome.data)
            with write_scope() as session:
                call = ToolCall(
                    task_id=ctx.task_id,
                    agent_run_id=ctx.agent_run_id,
                    tool_name=name,
                    arguments=_safe_args(arguments),
                    result_summary=_summarize(outcome),
                    result=safe_result,
                    result_digest=canonical_digest(safe_result),
                    status="success" if outcome.ok else "failed",
                    latency_ms=outcome.latency_ms,
                    policy_decision=outcome.policy_decision,
                    replay_policy=outcome.replay_policy,
                    side_effect_level=outcome.side_effect_level,
                    idempotency_key=outcome.idempotency_key,
                    replayed=outcome.replayed,
                    approval_reference=outcome.approval_reference,
                )
                session.add(call)
                session.flush()
                from app.services.task_execution import TaskExecutionService
                from app.models.workflow import AnalysisTask

                execution = TaskExecutionService(session)
                task_exists = session.get(AnalysisTask, ctx.task_id) is not None
                parent = None
                if task_exists and ctx.agent_run_id:
                    parent = execution.unit_for_owner(
                        task_id=ctx.task_id,
                        owner_type="agent_run",
                        owner_ref_id=ctx.agent_run_id,
                    )
                if task_exists:
                    unit = execution.start_unit(
                        task_id=ctx.task_id,
                        name=name,
                        unit_type="tool",
                        owner_type="tool_call",
                        owner_ref_id=call.id,
                        parent_unit_id=parent.id if parent else None,
                        failure_policy="continue",
                        metadata={
                            "replay_policy": outcome.replay_policy,
                            "side_effect_level": outcome.side_effect_level,
                            "replayed": outcome.replayed,
                        },
                    )
                    execution.finish_unit(
                        unit.id,
                        status="succeeded" if outcome.ok else "failed",
                        error=outcome.error,
                    )
        except Exception as exc:  # 审计写入失败不能影响主流程
            logger.warning("工具调用审计写入失败: %s", exc)

    @staticmethod
    def _validate_contract(spec: ToolSpec) -> None:
        if (
            spec.replay_policy is ReplayPolicy.SAFE
            and spec.side_effect_level is not SideEffectLevel.READ_ONLY
        ):
            raise ValueError(f"replay_policy=safe 仅允许只读工具: {spec.name}")
        if (
            spec.side_effect_level is SideEffectLevel.READ_ONLY
            and spec.replay_policy is not ReplayPolicy.SAFE
        ):
            raise ValueError(f"只读工具必须声明 replay_policy=safe: {spec.name}")
        if (
            spec.replay_policy is ReplayPolicy.IDEMPOTENT
            and not (spec.idempotency_scope or "").strip()
        ):
            raise ValueError(f"幂等工具必须声明 idempotency_scope: {spec.name}")
        if (
            spec.side_effect_level is SideEffectLevel.IRREVERSIBLE
            and not spec.requires_approval
        ):
            raise ValueError(f"不可逆工具必须要求人工审批: {spec.name}")


def _safe_args(arguments: dict) -> dict:
    """审计用的参数快照：截断长文本，避免把整篇文档写进审计表。"""
    from app.services.task_replay import sanitize_snapshot

    out: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > 500:
            out[key] = value[:500] + "…"
        else:
            out[key] = value
    return sanitize_snapshot(out)


def _summarize(outcome: ToolOutcome) -> str:
    if not outcome.ok:
        return f"失败: {outcome.error}"
    data = outcome.data
    if "hits" in data:
        return f"命中 {len(data['hits'])} 条切片"
    if "rows" in data:
        return f"返回 {len(data['rows'])} 行，能力={data.get('capability')}"
    return "ok"


_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """进程内单例，注册全部内置工具。"""
    global _registry
    if _registry is None:
        from app.tools.metric_tool import MetricQueryTool
        from app.tools.order_fact_tool import SPEC as ORDER_FACTS_SPEC
        from app.tools.retrieval_tool import RetrievalSearchTool

        registry = ToolRegistry()
        for tool in (RetrievalSearchTool(), MetricQueryTool()):
            registry.register(tool.spec())
        # 售后工单：只读订单事实查询（权限与只读由 PolicyEngine 在调用前校验）
        registry.register(ORDER_FACTS_SPEC)
        _registry = registry
    return _registry


def reset_tool_registry() -> None:
    global _registry
    _registry = None

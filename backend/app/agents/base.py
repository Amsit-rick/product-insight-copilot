"""Agent 受控骨架（设计文档 7.3 / 7.4 / 11 节）。

每个 Agent 都走同一条受控流水线：

    collect（只用白名单工具取数）
      -> deterministic_output（基于工具结果的确定性归纳，永远可用）
      -> model_refine（可选：模型在证据范围内归纳，输出必须是合法 JSON）
      -> validate（Pydantic Schema + 证据引用完整性 + 敏感内容扫描）
      -> persist（agent_runs 落库：模型、Prompt 版本、耗时、Token、错误、重试次数）

关键设计：
- 模型不可用时系统仍然产出（deterministic_output），但 `model_used=False` 如实记录；
- Agent 失败不会污染其他 Agent：异常被收敛为 `AgentResult(ok=False)`；
- 任何 findings 引用了不存在的证据都会被剔除，避免"无依据结论"进入报告。
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents.prompts import prompt_version, system_prompt
from app.agents.schemas import AgentInput, AgentResult, FindingCandidate
from app.config import Settings, get_settings
from app.db.session import write_scope
from app.logging_utils import get_logger
from app.models.enums import NodeStatus
from app.models.workflow import AgentRun
from app.services.model_gateway import LLMUsage, ModelGateway, get_model_gateway
from app.services.policy_engine import PolicyEngine, get_policy_engine
from app.tools.registry import ToolContext, ToolRegistry, get_tool_registry

logger = get_logger(__name__)

MAX_EVIDENCE_CHARS = 400
MAX_PROMPT_EVIDENCE = 12


class AgentTimeout(Exception):
    pass


class BudgetExceeded(Exception):
    """任务成本/token 预算超限且策略为 abort：直接失败，可重试（重试前可提高预算或换便宜模型）。"""


class ModelFinding(BaseModel):
    text: str = Field(min_length=1, max_length=1200)
    type: str = Field(default="fact", pattern="^(fact|inference|hypothesis|recommendation)$")
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    # 以下字段供后续阶段使用（跨来源推断、冲突溯源、报告渲染），必须能在 Schema 里存活
    id: str | None = Field(default=None, max_length=64)
    source_agent: str | None = Field(default=None, max_length=64)
    basis: str | None = Field(default=None, max_length=600)
    verify_by: str | None = Field(default=None, max_length=400)
    priority: str | None = Field(default=None, max_length=8)
    owner_role: str | None = Field(default=None, max_length=32)

# 结论在 Agent 之间传递时需要保留的附加字段
FINDING_PASSTHROUGH = (
    "id",
    "source_agent",
    "basis",
    "verify_by",
    "priority",
    "owner_role",
)


class AgentOutputModel(BaseModel):
    """所有 Agent 输出的公共形状。"""

    summary: str = Field(default="", max_length=2000)
    findings: list[ModelFinding] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)


@dataclass
class Collected:
    """collect 阶段的产物。"""

    evidence: list[dict] = field(default_factory=list)
    metric_results: list[dict] = field(default_factory=list)
    blocks: dict = field(default_factory=dict)
    data_gaps: list[str] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)

    def evidence_index(self) -> dict[str, dict]:
        return {item["local_ref"]: item for item in self.evidence if item.get("local_ref")}


class BaseAgent(ABC):
    """受控 Agent 基类。子类只需实现 collect / deterministic_output / user_prompt。"""

    code: ClassVar[str] = "base_agent"
    display_name: ClassVar[str] = "Agent"
    # 该 Agent 允许访问的知识库（与 PolicyEngine.AGENT_KBS 保持一致）
    kb_scope: ClassVar[set[str]] = set()
    output_model: ClassVar[type[BaseModel]] = AgentOutputModel
    # 证据局部编号前缀：E=文档证据，M=指标证据
    evidence_prefix: ClassVar[str] = "E"

    def __init__(
        self,
        *,
        gateway: ModelGateway | None = None,
        policy: PolicyEngine | None = None,
        registry: ToolRegistry | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.gateway = gateway or get_model_gateway()
        self.policy = policy or get_policy_engine()
        self.registry = registry or get_tool_registry()

    # ------------------------------------------------------------------ 主入口
    def run(self, ai: AgentInput) -> AgentResult:
        started = time.perf_counter()
        run_row = self._start_run(ai)
        attempts = 0
        last_error: str | None = None
        partial: AgentResult | None = None

        while attempts <= self.settings.agent_max_retries:
            attempts += 1
            try:
                result = self._attempt(ai, run_row, attempt=attempts)
                result.attempts = attempts
                result.latency_ms = int((time.perf_counter() - started) * 1000)
                if result.ok:
                    self._finish_run(run_row, result, NodeStatus.SUCCEEDED)
                    return result
                last_error = result.error
                partial = result
            except BudgetExceeded:
                # 预算超限（abort 策略）：不重试、不降级，直接向上抛，由执行器把任务标记为失败可重试
                run_row.status = NodeStatus.FAILED.value
                run_row.error = "budget_exceeded"
                run_row.finished_at = _utcnow()
                with write_scope() as session:
                    session.merge(run_row)
                    from app.services.task_execution import TaskExecutionService

                    TaskExecutionService(session).finish_owner(
                        task_id=run_row.task_id,
                        owner_type="agent_run",
                        owner_ref_id=run_row.id,
                        status="failed",
                        error="budget_exceeded",
                    )
                raise
            except AgentTimeout as exc:
                last_error = f"超时: {exc}"
            except Exception as exc:  # 任何异常都不允许击穿工作流
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("Agent %s 第 %s 次尝试失败: %s", self.code, attempts, last_error)
            if attempts <= self.settings.agent_max_retries:
                time.sleep(0.2 * attempts)

        # 全部尝试失败：保留已收集的结果，交给节点决定是否降级继续
        result = partial or AgentResult(agent_code=self.code, ok=False)
        result.ok = False
        result.error = last_error
        result.attempts = attempts
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        self._finish_run(run_row, result, NodeStatus.FAILED)
        return result

    # ------------------------------------------------------------------ 单次尝试
    def _attempt(self, ai: AgentInput, run_row: AgentRun, *, attempt: int) -> AgentResult:
        deadline = time.monotonic() + self.timeout_seconds(ai)
        run_row.attempt = attempt
        run_row.status = NodeStatus.RUNNING.value
        run_row.started_at = run_row.started_at or _utcnow()

        ctx = ToolContext(
            session=ai.session,
            task_id=ai.task_id,
            tenant_id=ai.tenant_id,
            user_id=ai.user_id,
            agent_code=self.code,
            max_security_rank=ai.max_security_rank,
            roles=list(ai.roles),
            allowed_kb_codes=set(ai.kb_codes),
            agent_run_id=run_row.id,
            budget=self.tool_budget(ai),
        )

        collected = self.collect(ai, ctx, deadline)
        self._check_deadline(deadline)

        fallback = self.deterministic_output(ai, collected)
        payload, usage = self._refine_with_model(ai, collected, fallback, deadline)

        output, schema_errors = self._validate_output(payload)
        findings = self._to_findings(output, collected)
        summary = self._sanitize(output.get("summary") or "")
        blocks = {
            key: value
            for key, value in output.items()
            if key not in {"summary", "findings", "data_gaps"}
        }
        data_gaps = list(output.get("data_gaps") or []) + list(collected.data_gaps)
        for err in collected.tool_errors:
            data_gaps.append(f"工具调用失败：{err}")

        confidence = self._aggregate_confidence(findings, collected)
        ok = bool(findings) or not collected.evidence and not collected.metric_results
        result = AgentResult(
            agent_code=self.code,
            ok=ok,
            summary=summary,
            findings=findings,
            evidence=collected.evidence,
            metric_results=collected.metric_results,
            blocks=blocks,
            confidence=confidence,
            data_gaps=list(dict.fromkeys(data_gaps)),
            tool_calls=list(ctx.calls),
            model_used=not usage.stub and bool(usage.model),
            model_name=usage.model,
            model_error=(usage.error if usage.stub else None),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cache_hit_tokens=usage.cache_hit_tokens,
            attempts=run_row.attempt,
            error=None if ok else "未获得任何可引用证据",
            schema_ok=not schema_errors,
            schema_errors=schema_errors,
            agent_run_id=run_row.id,
        )
        return result

    # ------------------------------------------------------------------ 子类实现
    @abstractmethod
    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        """只用白名单工具取数；不得直接访问数据库或外部网络。"""

    @abstractmethod
    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        """基于工具结果的确定性归纳：模型不可用时系统仍要产出。"""

    def tool_budget(self, ai: AgentInput) -> int:
        """本次运行的允许工具调用次数（子类可按计划规模放宽，但必须有上限）。"""
        return self.settings.agent_max_tool_calls

    def is_reflection_stage(self, ai: AgentInput) -> bool:
        return self.reflection_stage(ai) is not None

    def reflection_stage(self, ai: AgentInput) -> str | None:
        if self.code in {"revision_agent", "report_revision_agent"}:
            return "revision"
        if self.code == "report_reflection_agent" or (
            self.code == "review_agent" and bool(ai.filters.get("reflection_enabled"))
        ):
            return "reflection"
        return None

    def timeout_seconds(self, ai: AgentInput) -> float:
        return (
            float(self.settings.reflection_timeout_seconds)
            if self.is_reflection_stage(ai)
            else float(self.settings.agent_timeout_seconds)
        )

    def max_model_tokens(self, ai: AgentInput) -> int:
        return (
            int(self.settings.reflection_max_tokens)
            if self.is_reflection_stage(ai)
            else int(self.settings.agent_max_tokens)
        )

    def user_prompt(self, ai: AgentInput, collected: Collected) -> str:
        return self._default_user_prompt(ai, collected)

    # ------------------------------------------------------------------ 模型 refine
    def model_profile(self, ai: AgentInput | None = None):
        """本 Agent 的主模型目标：灰度 > 按 Agent 覆盖 > 全局默认。

        传 `ai.task_id` 是为了任务级灰度分桶——同一个任务稳定走同一侧。
        """
        return self.gateway.profile_for(self.code, sample_key=self._sample_key(ai))

    def model_chain(self, ai: AgentInput | None = None):
        """完整模型链：主模型（可能是灰度模型）→ 备用模型（按配置顺序）。"""
        return self.gateway.chain_for(self.code, sample_key=self._sample_key(ai))

    @staticmethod
    def _sample_key(ai: AgentInput | None) -> str | None:
        return getattr(ai, "task_id", None) if ai is not None else None

    def budget_check(self, ai: AgentInput) -> dict:
        """调用模型前检查任务预算（token / 金额上限）。

        预算判断必须在"发请求之前"做：一旦超限就不要再花钱，
        而是退回确定性归纳，并在报告里说明本次因预算限制未使用模型。
        """
        from app.services.cost import budget_state, reflection_budget_state

        try:
            task_state = budget_state(ai.session, ai.task_id, self.settings)
            if task_state.get("exceeded") or not self.is_reflection_stage(ai):
                return task_state
            reflection_state = reflection_budget_state(ai.session, ai.task_id, self.settings)
            if reflection_state.get("exceeded"):
                return reflection_state
            return task_state
        except Exception as exc:  # 预算查询失败不应阻断主流程
            logger.warning("预算检查失败（按未超限处理）: %s", exc)
            return {"exceeded": False, "reasons": [], "action": "degrade", "limits": {}}

    def _refine_with_model(
        self,
        ai: AgentInput,
        collected: Collected,
        fallback: dict,
        deadline: float,
    ) -> tuple[dict, Any]:
        self._check_deadline(deadline)
        if ai.filters.get("multi_agent") is False:
            # 低风险快路径仅使用规则结果和核验到的事实，避免为了润色调用多个模型。
            return fallback, LLMUsage(model="deterministic", stub=True)
        budget = self.budget_check(ai)
        if budget.get("exceeded"):
            reason = "；".join(budget.get("reasons") or []) or "任务预算已超限"
            if budget.get("action") == "abort":
                raise BudgetExceeded(reason)
            logger.warning("预算超限，%s 跳过模型调用：%s", self.code, reason)
            collected.data_gaps.append(f"因成本预算限制，本次未使用模型：{reason}")
            return fallback, LLMUsage(
                model=self.model_profile(ai).model, stub=True, error=f"budget_exceeded: {reason}"
            )

        chain = self.model_chain(ai)
        prompt = self.user_prompt(ai, collected)
        from app.skills.after_sales import skill_prompt_for_agent

        system_instruction = system_prompt(self.code) + skill_prompt_for_agent(
            self.code, ai.filters.get("business_skills")
        )
        payload, usage = self.gateway.chat_json(
            system_instruction,
            prompt,
            fallback=lambda: fallback,
            max_tokens=min(chain[0].max_output_tokens, self.max_model_tokens(ai)),
            profile=chain[0],
            chain=chain,
        )
        if usage.stub:
            # 模型未启用或输出不可用：如实降级，不伪装成模型结论
            return fallback, usage
        merged = dict(fallback)
        if isinstance(payload, dict) and payload:
            merged.update({k: v for k, v in payload.items() if v not in (None, "", [], {})})
        return merged, usage

    def _default_user_prompt(self, ai: AgentInput, collected: Collected) -> str:
        evidence_lines = []
        for item in collected.evidence[:MAX_PROMPT_EVIDENCE]:
            content = (item.get("content") or "")[:MAX_EVIDENCE_CHARS]
            evidence_lines.append(
                f"[{item.get('local_ref')}] 来源={item.get('source_name')} "
                f"页码={item.get('page')} 版本={item.get('document_version')}\n{content}"
            )
        for item in collected.metric_results:
            evidence_lines.append(
                f"[{item.get('local_ref')}] 指标={item.get('metric_name')} "
                f"能力={item.get('capability')} 摘要={json.dumps(item.get('summary'), ensure_ascii=False)} "
                f"数据版本={item.get('dataset_version')}{self._render_rows(item)}"
            )
        return (
            f"<问题>{ai.query}</问题>\n"
            f"<范围>产品={ai.filters.get('product')} 平台={ai.platform} "
            f"版本={ai.app_version} 时间={ai.date_from}~{ai.date_to}</范围>\n"
            f"<证据>\n" + ("\n\n".join(evidence_lines) or "（无检索结果）") + "\n</证据>"
        )

    @staticmethod
    def _render_rows(item: dict) -> str:
        """把指标行数据带进提示词。

        这一步很关键：只给模型"占比最高是 X"这类汇总，它就**无法**回答
        "哪个分群数值最差"——实测模型会因此在报告里写"缺少各组数值，无法定位人群"。
        所以这里把分群/趋势明细按行喂进去（限量，避免提示词膨胀）。
        """
        rows = item.get("rows") or []
        if not rows:
            return ""
        limited = rows[:10]
        rendered = json.dumps(limited, ensure_ascii=False, default=str)
        more = f"（共 {len(rows)} 行，此处取前 {len(limited)} 行）" if len(rows) > len(limited) else ""
        return f"\n分群/趋势行数据{more}：{rendered}"

    # ------------------------------------------------------------------ 校验与清洗
    def _validate_output(self, payload: dict) -> tuple[dict, list[str]]:
        """Pydantic Schema 校验；失败时退回最小合法结构，但记录 schema_errors。"""
        # 模型偶尔把 data_gaps 写成字符串（"未检索到..."），而契约是 list[str]。
        # 这个字段只是诊断信息，不值得因为类型形状丢掉整段 findings/policies；
        # 先做保守归一，再走强类型校验。findings 同理，非 list 直接视为空。
        payload = dict(payload or {})
        gaps = payload.get("data_gaps")
        if not isinstance(gaps, list):
            payload["data_gaps"] = [] if gaps in (None, "") else [str(gaps)]
        if not isinstance(payload.get("findings"), list):
            payload["findings"] = []
        try:
            model = self.output_model.model_validate(payload)
            return model.model_dump(), []
        except ValidationError as exc:
            errors = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()][:10]
            logger.warning("Agent %s 输出未通过 Schema 校验: %s", self.code, errors)
            safe = {
                "summary": str(payload.get("summary") or "")[:2000],
                "findings": [],
                "data_gaps": [f"输出校验失败：{e}" for e in errors],
            }
            return safe, errors

    def _to_findings(self, output: dict, collected: Collected) -> list[dict]:
        """把已验证的输出转成候选陈述，并剔除引用了不存在证据的结论。"""
        known = set(collected.evidence_index())
        findings: list[dict] = []
        dropped = 0
        for index, item in enumerate(output.get("findings") or [], start=1):
            if isinstance(item, BaseModel):
                item = item.model_dump()
            if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                continue
            refs = [str(r) for r in (item.get("evidence_refs") or [])]
            valid_refs = [r for r in refs if r in known]
            if refs and not valid_refs:
                # 结论声称有证据但证据不存在：直接丢弃，避免无依据结论进入报告
                dropped += 1
                continue
            findings.append(
                {
                    **FindingCandidate(
                        statement_id=f"{self.code}-{index}",
                        kind=str(item.get("type") or "fact"),
                        text=self._sanitize(str(item["text"]))[:1200],
                        evidence_refs=valid_refs,
                        confidence=float(item.get("confidence") or 0.6),
                        tags=[self.code],
                    ).as_dict(),
                    # 附加字段透传：否则跨来源推断/冲突溯源会丢失来源信息
                    **{
                        key: item[key]
                        for key in FINDING_PASSTHROUGH
                        if item.get(key) is not None
                    },
                }
            )
        if dropped:
            logger.warning("Agent %s 有 %s 条结论因证据引用无效被剔除", self.code, dropped)
        return findings

    def _sanitize(self, text: str) -> str:
        """敏感内容扫描：命中即打码，并保留痕迹供审计。"""
        hit = self.policy.scan_output(text)
        if hit:
            return text.replace(hit, "[已过滤]")
        return text

    def _aggregate_confidence(self, findings: list[dict], collected: Collected) -> float:
        if not findings:
            return 0.0
        base = sum(float(f.get("confidence") or 0.5) for f in findings) / len(findings)
        # 证据越多、被引用越多，置信度越接近上限
        refs = {r for f in findings for r in f.get("evidence_refs") or []}
        coverage = min(1.0, len(refs) / max(1, len(findings) * 1.5))
        return round(min(0.95, base * (0.7 + 0.3 * coverage)), 4)

    # ------------------------------------------------------------------ 工具与持久化
    def call_tool(self, ctx: ToolContext, name: str, arguments: dict) -> dict:
        """统一工具入口：策略校验 + 预算 + 审计都在注册表里完成。"""
        outcome = self.registry.call(name, arguments, ctx)
        if not outcome.ok:
            raise ToolCallDenied(outcome.error or "工具调用失败", outcome.policy_decision)
        return outcome.data

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        if time.monotonic() > deadline:
            raise AgentTimeout("超过单节点执行时限")

    def _canary_bucket_of(self, task_id: str | None) -> int | None:
        """该任务落在哪个灰度桶（0-99）；未配置灰度或没有 task_id 时为 None。

        有了它就能离线复算"某个任务本该走哪一侧"，不必翻日志猜。
        """
        if not (self.settings.model_canary or {}) or not task_id:
            return None
        return self.gateway.canary_bucket(str(task_id))

    def _start_run(self, ai: AgentInput) -> AgentRun:
        # 记录**该 Agent 配置的模型**（未配置时为 stub），便于审计"谁用了哪个模型"；
        # 实际是否用上了模型输出，看 output.model_used。
        profile = self.model_profile(ai)
        row = AgentRun(
            task_id=ai.task_id,
            agent_code=self.code,
            node=self.code,
            status=NodeStatus.RUNNING.value,
            attempt=1,
            model=profile.model if profile.configured else "stub",
            prompt_version=prompt_version(),
            input_summary=f"{ai.query[:200]} | kb={sorted(ai.kb_codes)}",
            started_at=_utcnow(),
        )
        # 非映射字段，只用于把"这次走的是灰度还是对照"带到收尾时写进 output
        row._profile_source = profile.source
        row._reflection_stage = self.reflection_stage(ai)
        row._reflection_rollout = (
            ai.filters.get("report_reflection_rollout")
            or ai.filters.get("reflection_rollout")
            or None
        )
        from app.skills.after_sales import skills_for_agent

        row._business_skills = skills_for_agent(self.code, ai.filters.get("business_skills"))
        # 用短写事务落库并立即提交：Agent 运行记录不能和"模型调用"共用一个长事务，
        # 否则 4 个并行 Agent 会把 SQLite 写锁各自握上好几秒，互相锁死。
        with write_scope() as session:
            session.add(row)
            session.flush()
            from app.services.task_execution import TaskExecutionService

            TaskExecutionService(session).start_unit(
                task_id=ai.task_id,
                name=self.display_name,
                unit_type="agent",
                owner_type="agent_run",
                owner_ref_id=row.id,
                failure_policy="continue",
                metadata={
                    "agent_code": self.code,
                    "node": self.code,
                    "model": row.model,
                },
            )
            session.refresh(row)
            session.expunge(row)
        return row

    def _finish_run(self, row: AgentRun, result: AgentResult, status: NodeStatus) -> None:
        row.status = status.value
        structured: dict = {}
        if result.blocks:
            for key in (
                "verdict",
                "required_changes",
                "missing_evidence",
                "stop_reason",
                "addressed_issue_codes",
                "remaining_issue_codes",
                "changed_sections",
            ):
                if key in result.blocks:
                    structured[key] = result.blocks[key]
            if result.blocks.get("issues"):
                structured["issues"] = [
                    {
                        key: item.get(key)
                        for key in ("code", "severity", "category", "message", "auto_fixable")
                    }
                    for item in result.blocks["issues"]
                    if isinstance(item, dict)
                ]
        row.output = {
            "summary": result.summary,
            "findings": result.findings,
            "confidence": result.confidence,
            "data_gaps": result.data_gaps,
            "schema_ok": result.schema_ok,
            "schema_errors": result.schema_errors,
            "model_used": result.model_used,
            "model_name": result.model_name,
            "model_error": result.model_error,
            "business_skills": getattr(row, "_business_skills", []),
            # 路由来源：canary=灰度 / override=按 Agent 覆盖 / global=全局默认。
            # 灰度期间"这条记录走了哪一侧"必须可查，否则 A/B 结果无法归因。
            "model_source": getattr(row, "_profile_source", None) or "unknown",
            "canary_bucket": self._canary_bucket_of(row.task_id),
            # 供应商侧 prompt 缓存命中量：计费单价更低，成本核算要用
            "cache_hit_tokens": result.cache_hit_tokens,
            # generation/reflection/revision 三类阶段独立统计；不保存 Prompt 或思维链。
            "execution_stage": getattr(row, "_reflection_stage", None) or "generation",
            "reflection_stage": getattr(row, "_reflection_stage", None),
            "reflection_rollout": getattr(row, "_reflection_rollout", None),
            # 只保留可审计的结构化结论，不保存 Prompt、原始回复或模型思维链。
            "structured": structured,
        }
        row.latency_ms = result.latency_ms
        row.prompt_tokens = result.prompt_tokens
        row.completion_tokens = result.completion_tokens
        row.total_tokens = result.prompt_tokens + result.completion_tokens
        row.error = result.error
        row.finished_at = _utcnow()
        if result.model_name:
            # 实际调用的模型名（有降级时为最终起作用的那一个）
            row.model = result.model_name
        # 收尾同样走短写事务（此时 row 已与任何会话解绑，按 id 更新）
        with write_scope() as session:
            merged = session.merge(row)
            session.flush()
            from app.services.task_execution import TaskExecutionService

            TaskExecutionService(session).finish_owner(
                task_id=row.task_id,
                owner_type="agent_run",
                owner_ref_id=row.id,
                status="succeeded" if status == NodeStatus.SUCCEEDED else "failed",
                error=result.error,
            )
            _ = merged


class ToolCallDenied(Exception):
    def __init__(self, message: str, decision: str = "deny") -> None:
        super().__init__(message)
        self.decision = decision


def _utcnow():
    from app.models.base import utcnow

    return utcnow()


def make_evidence(
    hits: list[dict], *, agent_code: str, prefix: str = "E", start: int = 1
) -> list[dict]:
    """给检索命中打上局部证据编号，供结论引用与后续全局重映射。"""
    evidence: list[dict] = []
    for offset, hit in enumerate(hits):
        item = dict(hit)
        item["local_ref"] = f"{prefix}{start + offset}"
        item["agent_code"] = agent_code
        item["source_kind"] = item.get("source_kind") or "document"
        evidence.append(item)
    return evidence


def metric_evidence(result: dict, *, agent_code: str, ref: str) -> dict:
    """把指标结果包装成证据对象（可定位到查询参数与数据版本）。"""
    rows = result.get("rows") or []
    return {
        "local_ref": ref,
        "agent_code": agent_code,
        "source_kind": "metric",
        "metric_name": result.get("metric_name"),
        "capability": result.get("capability"),
        "params": result.get("params"),
        "summary": result.get("summary"),
        # 行数据不进 state（体积大），但保留行数用于充分性与对齐判断
        "row_count": len(rows),
        "dataset_version": result.get("dataset_version"),
        "source_version": result.get("source_version"),
        "quality_flags": result.get("quality_flags") or [],
        "warnings": result.get("warnings") or [],
        "content": _metric_sentence(result),
        "source_name": f"指标服务:{result.get('metric_name')}",
        "relevance_score": 1.0,
        "permission_checked": True,
    }


def _metric_sentence(result: dict) -> str:
    summary = result.get("summary") or {}
    parts = [f"{result.get('metric_name')}（{result.get('capability')}）"]
    for key in ("start_value", "end_value", "delta", "pct_change", "baseline_avg", "target_avg"):
        if summary.get(key) is not None:
            parts.append(f"{key}={summary[key]}")
    if summary.get("anomaly_count") is not None:
        parts.append(f"异常点={summary['anomaly_count']}")
    if result.get("warnings"):
        parts.append("警告：" + "；".join(str(w) for w in result["warnings"]))
    return "，".join(parts)

"""报告 Agent：汇总已校验事实与建议。

纪律（设计文档 7.3 / 10 节）：
- 只使用结构化中间结果（已重映射为全局证据编号的陈述），不引入未检索事实；
- 事实 / 推断 / 假设 / 建议四类必须分开；
- 冲突原样保留，不强行合并；
- 报告 Agent 没有任何工具权限，`kb_scope` 为空。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.base import AgentOutputModel, BaseAgent, Collected
from app.agents.schemas import AgentInput
from app.tools.registry import ToolContext


class DraftImpact(BaseModel):
    dimension: str = Field(max_length=32)
    value: str = Field(max_length=160)
    evidence_refs: list[str] = Field(default_factory=list)


class DraftHypothesis(BaseModel):
    text: str = Field(max_length=800)
    verify_by: str = Field(default="", max_length=400)
    evidence_refs: list[str] = Field(default_factory=list)


class DraftRecommendation(BaseModel):
    text: str = Field(max_length=800)
    priority: str = Field(default="P2", pattern="^(P0|P1|P2|P3)$")
    owner_role: str = Field(default="product", max_length=32)
    evidence_refs: list[str] = Field(default_factory=list)


class ReportAgentOutput(AgentOutputModel):
    impact_scope: list[DraftImpact] = Field(default_factory=list)
    hypotheses: list[DraftHypothesis] = Field(default_factory=list)
    recommendations: list[DraftRecommendation] = Field(default_factory=list)
    priority: str = Field(default="P2", pattern="^(P0|P1|P2|P3)$")
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class ReportAgent(BaseAgent):
    code = "report_agent"
    display_name = "报告 Agent"
    kb_scope: set[str] = set()
    output_model = ReportAgentOutput

    # ------------------------------------------------------------------ 输入装配
    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        """报告 Agent 不取数：输入只能来自已经校验过的结构化中间结果。"""
        digest = ai.plan_item.get("digest") or {}
        collected = Collected()
        # 证据的 local_ref 直接用全局证据编号，保证报告里的引用在合并后依然有效
        collected.evidence = [
            {**item, "local_ref": item.get("evidence_id")}
            for item in (digest.get("evidence") or [])
            if item.get("evidence_id")
        ]
        collected.metric_results = [
            item for item in collected.evidence if item.get("source_kind") == "metric"
        ]
        collected.blocks = {
            "statements": digest.get("statements") or [],
            "agents": digest.get("agents") or {},
            "conflicts": digest.get("conflicts") or [],
            "coverage": digest.get("coverage") or {},
            "intent": digest.get("intent"),
            "filters": ai.filters,
        }
        if not collected.evidence:
            collected.data_gaps.append("没有可用于生成报告的证据")
        return collected

    # ------------------------------------------------------------------ 模型输入
    def user_prompt(self, ai: AgentInput, collected: Collected) -> str:
        """给模型的任务刻意收窄：只写"表达层"，不重述已校验的事实。

        为什么这么做：报告的结构化主体（事实/推断/假设）已经由前面的 Agent 产出并
        通过了证据校验，让模型再复述一遍既不会更准，又会把 JSON 撑到 max_tokens 被截断
        （实测报告的 facts 一多，模型输出就会在 JSON 中途断掉，最终只能整段丢弃）。
        模型只负责它真正擅长的部分：摘要措辞、优先级、置信度与行动建议。
        """
        base = self._default_user_prompt(ai, collected)
        return (
            f"{base}\n\n"
            "本轮只需要你输出下面这几个字段，**不要重复输出 facts / inferences / hypotheses**"
            "（它们已由上游校验过的证据确定，系统会沿用）：\n"
            '{"summary": "结论文本", "priority": "P0|P1|P2|P3", "confidence": 0.0-1.0, '
            '"recommendations": [{"text": "行动建议", "priority": "P0|P1|P2|P3", '
            '"owner_role": "product|engineering|operation|data", "evidence_refs": ["E1"]}], '
            '"impact_scope": [{"dimension": "platform|version|channel|device", "value": "取值", '
            '"evidence_refs": ["E1"]}]}\n'
            "要求：摘要必须明确区分事实与不确定性；存在证据冲突时如实说明；"
            "建议必须可执行，并引用真实存在的证据编号。"
        )

    # ------------------------------------------------------------------ 确定性汇总
    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        digest = ai.plan_item.get("digest") or {}
        statements: list[dict] = list(digest.get("statements") or [])
        conflicts: list[dict] = list(digest.get("conflicts") or [])
        coverage: dict = digest.get("coverage") or {}
        valid_refs = {item.get("local_ref") for item in collected.evidence}

        facts: list[dict] = []
        inferences: list[dict] = []
        hypotheses: list[dict] = []
        seen_texts: set[str] = set()

        for statement in statements:
            kind = statement.get("type") or "fact"
            refs = [r for r in (statement.get("evidence_refs") or []) if r in valid_refs]
            text = str(statement.get("text") or "").strip()
            if not text:
                continue
            # 同一句结论可能被多个指标查询重复推出，去重后再进报告
            fingerprint = f"{kind}:{text}"
            if fingerprint in seen_texts:
                continue
            seen_texts.add(fingerprint)
            if kind == "fact":
                facts.append(
                    {
                        "id": f"fact-{len(facts) + 1}",
                        "type": "fact",
                        "text": text,
                        "evidence_refs": refs,
                        "confidence": statement.get("confidence", 0.6),
                        "source_agent": statement.get("source_agent"),
                    }
                )
                if statement.get("supplemental"):
                    facts[-1]["tags"] = ["supplemental"]
            elif kind == "inference":
                inferences.append(
                    {
                        "id": f"inf-{len(inferences) + 1}",
                        "type": "inference",
                        "text": text,
                        "basis": statement.get("basis")
                        or f"来自 {statement.get('source_agent')} 的归纳",
                        "evidence_refs": refs,
                        "confidence": statement.get("confidence", 0.6),
                        "source_agent": statement.get("source_agent"),
                    }
                )
            elif kind == "hypothesis":
                hypotheses.append(
                    {
                        "id": f"hyp-{len(hypotheses) + 1}",
                        "type": "hypothesis",
                        "text": text,
                        "verify_by": statement.get("verify_by") or "",
                        "evidence_refs": refs,
                    }
                )

        # 影响范围：来自指标证据的维度信息与分群结果
        impact: list[dict] = []
        seen_impact: set[tuple] = set()
        for item in collected.metric_results:
            params = item.get("params") or {}
            for dimension in ("platform", "app_version", "channel", "device"):
                value = params.get(dimension)
                if not value:
                    continue
                key = (dimension, str(value))
                if key in seen_impact:
                    continue
                seen_impact.add(key)
                impact.append(
                    {
                        "dimension": dimension,
                        "value": str(value),
                        "evidence_refs": [item.get("local_ref")],
                    }
                )
            summary = item.get("summary") or {}
            if item.get("capability") == "breakdown" and summary.get("top_key"):
                key = (str(summary.get("dimension") or "segment"), str(summary.get("top_key")))
                if key in seen_impact:
                    continue
                seen_impact.add(key)
                impact.append(
                    {
                        "dimension": key[0],
                        # 明确口径：这是"占总量最大"的分组，不是"跌幅最大"的分组
                        "value": f"{key[1]}（占总量 {float(summary.get('top_share') or 0):.1%}，"
                        f"总量口径）",
                        "evidence_refs": [item.get("local_ref")],
                    }
                )

        # 跨来源推断：只有同时存在版本对比事实与反馈/研发事实时才生成
        version_facts = [f for f in facts if "版本对比" in f["text"] or "发布日切分" in f["text"]]
        feedback_facts = [f for f in facts if f.get("source_agent") == "feedback_agent"]
        release_facts = [f for f in facts if f.get("source_agent") == "release_agent"]
        if version_facts and (feedback_facts or release_facts):
            refs = sorted(
                {
                    *version_facts[0].get("evidence_refs", []),
                    *((feedback_facts or release_facts)[0].get("evidence_refs") or []),
                }
            )
            inferences.append(
                {
                    "id": f"inf-{len(inferences) + 1}",
                    "type": "inference",
                    "text": "指标在版本发布后出现下降，同时存在同期的反馈集中主题与版本变更记录，"
                    "三者时间上重合，指向版本相关性；但时间重合不等于因果，仍需对照灰度与实验分组。",
                    "basis": "版本对比事实 + 反馈/版本变更事实",
                    "evidence_refs": refs,
                    "confidence": 0.66,
                    "source_agent": "report_agent",
                }
            )

        if not hypotheses:
            if version_facts and (feedback_facts or release_facts):
                refs = sorted(version_facts[0].get("evidence_refs") or [])
                hypotheses.append(
                    {
                        "id": "hyp-1",
                        "type": "hypothesis",
                        "text": "假设：版本中的核心链路改造（登录/启动/SDK 等）在低端设备上引入失败"
                        "或耗时上升，导致新用户次日回访减少。",
                        "verify_by": "按设备档位与渠道拆分登录成功率与启动耗时，核对灰度批次，"
                        "并对低端机做版本回退对照实验。",
                        "evidence_refs": refs,
                    }
                )

        recommendations = self._build_recommendations(
            version_facts, feedback_facts, conflicts, coverage
        )
        priority = self._decide_priority(version_facts, feedback_facts, conflicts, coverage)
        confidence = self._compute_confidence(collected, conflicts, coverage)
        summary = self._build_summary(ai, facts, inferences, conflicts, coverage, priority, confidence)

        return {
            "summary": summary,
            "findings": [*facts, *inferences],
            "impact_scope": impact,
            "hypotheses": hypotheses,
            "recommendations": recommendations,
            "priority": priority,
            "confidence": confidence,
            "data_gaps": list(coverage.get("data_gaps") or []),
        }

    # ------------------------------------------------------------------ 规则
    @staticmethod
    def _build_recommendations(
        version_facts: list[dict],
        feedback_facts: list[dict],
        conflicts: list[dict],
        coverage: dict,
    ) -> list[dict]:
        recommendations: list[dict] = []
        if version_facts:
            refs = sorted({r for f in version_facts for r in f.get("evidence_refs") or []})
            recommendations.append(
                {
                    "text": "对受影响版本做分渠道、分设备档位的对照验证（灰度回退或版本回滚评估），"
                    "确认指标下降是否随版本消失。",
                    "priority": "P0" if conflicts else "P1",
                    "owner_role": "engineering",
                    "evidence_refs": refs,
                }
            )
        if feedback_facts:
            refs = sorted({r for f in feedback_facts[:3] for r in f.get("evidence_refs") or []})
            recommendations.append(
                {
                    "text": "针对反馈集中的主题做日志级定位（崩溃栈、登录失败码），先确认是否存在"
                    "可复现的用户可感知故障，再决定是否热修。",
                    "priority": "P1",
                    "owner_role": "engineering",
                    "evidence_refs": refs,
                }
            )
        if conflicts:
            recommendations.append(
                {
                    "text": "技术侧监控结论与用户反馈存在冲突，先由研发与客服共同复盘原始日志与工单，"
                    "确认监控口径是否覆盖低端机与启动阶段，再对外给出结论。",
                    "priority": "P1",
                    "owner_role": "product",
                    "evidence_refs": sorted(
                        {r for c in conflicts for r in (c.get("evidence_refs") or [])}
                    ),
                }
            )
        gaps = list(coverage.get("data_gaps") or [])
        if gaps:
            recommendations.append(
                {
                    "text": "补齐缺失数据后复跑诊断：" + "；".join(str(g) for g in gaps[:3]),
                    "priority": "P2",
                    "owner_role": "data",
                    "evidence_refs": [],
                }
            )
        if not recommendations:
            recommendations.append(
                {
                    "text": "现有证据不足以支撑行动建议，建议先补充文档与指标数据后重新发起诊断。",
                    "priority": "P2",
                    "owner_role": "product",
                    "evidence_refs": [],
                }
            )
        for index, item in enumerate(recommendations, start=1):
            item["id"] = f"rec-{index}"
        return recommendations

    @staticmethod
    def _decide_priority(
        version_facts: list[dict],
        feedback_facts: list[dict],
        conflicts: list[dict],
        coverage: dict,
    ) -> str:
        if conflicts:
            return "P1"
        if version_facts and feedback_facts:
            return "P1"
        if version_facts or feedback_facts:
            return "P2"
        if coverage.get("data_gaps"):
            return "P3"
        return "P2"

    @staticmethod
    def _compute_confidence(
        collected: Collected, conflicts: list[dict], coverage: dict
    ) -> float:
        base = min(0.9, 0.35 + 0.05 * len(collected.evidence))
        base -= 0.12 * len(conflicts)
        base -= 0.03 * len(coverage.get("data_gaps") or [])
        if coverage.get("metric_quality_flags"):
            base -= 0.05
        if not coverage.get("metric_available", True):
            base -= 0.05
        return round(max(0.15, min(0.95, base)), 4)

    @staticmethod
    def _build_summary(
        ai: AgentInput,
        facts: list[dict],
        inferences: list[dict],
        conflicts: list[dict],
        coverage: dict,
        priority: str,
        confidence: float,
    ) -> str:
        if not facts:
            return (
                "现有证据不足以给出确定结论：未检索到可直接支撑诊断的文档或指标数据。"
                "建议补充材料后重新发起诊断。"
            )
        parts = [
            f"围绕「{ai.query[:40]}」共形成 {len(facts)} 条事实、{len(inferences)} 条推断。",
            f"首要事实：{facts[0]['text']}",
        ]
        if conflicts:
            parts.append(f"存在 {len(conflicts)} 处证据冲突，未做强行合并。")
        if not coverage.get("metric_available", True):
            parts.append("本次未取得可用的指标数据支撑，结论以文本证据为主。")
        parts.append(f"建议优先级 {priority}，整体置信度 {confidence:.2f}。")
        return "".join(parts)

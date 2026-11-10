"""指标解读 Agent：解释 MetricService 的结果，永远不生成 SQL。

职责边界（设计文档 7.3 / 8.3 节）：
- 只能调用 metric.query 的四个固定能力；
- 必须报告数据版本、样本量、质量标记与数据缺失；
- 指标事实与业务原因严格分开：原因只能写成假设。
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, Field

from app.agents.base import (
    AgentOutputModel,
    BaseAgent,
    Collected,
    make_evidence,
    metric_evidence,
)
from app.agents.schemas import AgentInput
from app.models.enums import KnowledgeBaseCode
from app.tools.registry import ToolContext

DEFAULT_METRICS = ("next_day_retention",)


def _direction_word(delta: float | None) -> str:
    """把数值变化翻译成方向词：指标事实必须能直接读出方向（也便于冲突检测）。"""
    if delta is None:
        return "无变化"
    if delta > 0:
        return "上升"
    if delta < 0:
        return "下降"
    return "持平"


class MetricObservationItem(BaseModel):
    metric_name: str = Field(max_length=64)
    capability: str = Field(max_length=32)
    direction: str = Field(default="unknown", pattern="^(up|down|flat|unknown)$")
    magnitude: float | None = None
    scope: str = Field(default="", max_length=200)
    dataset_version: str | None = None
    warnings: list[str] = Field(default_factory=list)


class MetricAgentOutput(AgentOutputModel):
    observations: list[MetricObservationItem] = Field(default_factory=list)


class MetricAgent(BaseAgent):
    code = "metric_agent"
    display_name = "指标解读 Agent"
    # 运营知识库（指标定义 / 实验记录 / 报表说明）由指标 Agent 负责：
    # 指标数值必须结合口径与实验重叠来解释，否则很容易把"实验分流"读成"版本影响"
    kb_scope = {KnowledgeBaseCode.OPERATION.value}
    output_model = MetricAgentOutput
    evidence_prefix = "M"

    def tool_budget(self, ai: AgentInput) -> int:
        """指标 Agent 的预算按计划中的查询条数确定：计划由代码生成，条数天生有界。

        默认预算（agent_max_tool_calls）是防"Agent 自己无限取数"的闸门，
        而这里每一个查询都来自 `create_plan`，因此按其规模放宽，但仍设上限。
        """
        planned = len(ai.plan_item.get("metric_queries") or [])
        return min(16, max(self.settings.agent_max_tool_calls, planned + 1))

    # ------------------------------------------------------------------ 取数
    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        collected = Collected()
        queries = list(ai.plan_item.get("metric_queries") or [])
        if not queries:
            queries = self._default_queries(ai)

        # 先读运营知识库：指标口径、实验记录、报表说明决定了"这个数字该怎么解释"
        kb_codes = sorted(self.kb_scope & ai.kb_codes)
        if kb_codes:
            try:
                data = self.call_tool(
                    ctx,
                    "retrieval.search",
                    {
                        "query": ai.sub_query("指标口径 定义 实验记录 报表说明"),
                        "knowledge_bases": kb_codes,
                        "top_k": 6,
                    },
                )
                hits = (data.get("hits") or [])[:6]
                collected.evidence.extend(make_evidence(hits, agent_code=self.code))
                if not hits:
                    collected.data_gaps.append("运营知识库中没有检索到指标口径或实验记录")
            except Exception as exc:
                collected.tool_errors.append(f"operation_kb 检索失败: {exc}")
        else:
            collected.data_gaps.append("当前角色没有运营知识库权限，无法核对指标口径")

        if not queries:
            collected.data_gaps.append("缺少可用于指标查询的时间范围或指标名")
            return collected

        ref_index = 1
        for query in queries:
            capability = query.get("capability")
            params = dict(query.get("params") or {})
            try:
                result = self.call_tool(
                    ctx, "metric.query", {"capability": capability, "params": params}
                )
            except Exception as exc:
                collected.tool_errors.append(f"指标查询 {capability} 失败: {exc}")
                collected.data_gaps.append(f"指标能力 {capability} 未取到数据（{exc}）")
                continue
            evidence = metric_evidence(
                result, agent_code=self.code, ref=f"{self.evidence_prefix}{ref_index}"
            )
            ref_index += 1
            collected.evidence.append(evidence)
            # evidence 里只保留 row_count（避免 state 膨胀），
            # 但本 Agent 的结论推导需要完整行数据，因此单独留一份。
            collected.metric_results.append({**evidence, "rows": result.get("rows") or []})
            # 异常检测的 rows 是"异常点"：没有异常点不等于没有数据
            summary = result.get("summary") or {}
            available = bool(result.get("rows")) or (
                capability == "detect_anomaly" and bool(summary.get("points"))
            )
            if result.get("policy_denied"):
                # 密级拒绝 ≠ 数据缺失：必须写成"无权访问"，否则报告会给出一个错误的"数据缺失"结论
                reason = result.get("denied_reason") or "指标密级不足"
                collected.data_gaps.append(
                    f"{result.get('metric_name')}: 无权限读取该指标（{reason}），本次未纳入分析"
                )
            elif not available:
                for warning in result.get("warnings") or ["指标数据缺失"]:
                    collected.data_gaps.append(f"{result.get('metric_name')}: {warning}")
            self._check_deadline(deadline)
        return collected

    def _default_queries(self, ai: AgentInput) -> list[dict]:
        """没有计划时按问题类型退化为最小可用查询集。"""
        metrics = list(ai.filters.get("metrics") or DEFAULT_METRICS)
        start = ai.date_from
        end = ai.date_to
        if not start or not end:
            # 未给时间范围时，默认观察最近 30 天
            today = date.today()
            start = start or (today - timedelta(days=30)).isoformat()
            end = end or today.isoformat()
        platform = ai.platform
        version = ai.app_version
        baseline = ai.filters.get("baseline_version")
        queries: list[dict] = []
        for metric in metrics:
            base = {
                "metric_name": metric,
                "start_date": str(start),
                "end_date": str(end),
            }
            if platform:
                base["platform"] = platform
            if ai.filters.get("channel"):
                base["channel"] = ai.filters["channel"]
            if ai.filters.get("device"):
                base["device"] = ai.filters["device"]
            if version and baseline:
                queries.append(
                    {
                        "capability": "compare_versions",
                        "params": {
                            **base,
                            "baseline_version": baseline,
                            "target_version": version,
                            "release_date": ai.filters.get("release_date"),
                        },
                    }
                )
            queries.append(
                {
                    "capability": "get_trend",
                    "params": {**base, "app_version": version} if version else dict(base),
                }
            )
            queries.append(
                {
                    "capability": "breakdown",
                    "params": {
                        **base,
                        "dimension": "channel",
                        "app_version": version,
                    }
                    if version
                    else {**base, "dimension": "channel"},
                }
            )
            queries.append(
                {
                    "capability": "detect_anomaly",
                    "params": {**base, "app_version": version, "method": "zscore", "threshold": 2.0}
                    if version
                    else {**base, "method": "zscore", "threshold": 2.0},
                }
            )
        return queries[:6]

    # ------------------------------------------------------------------ 确定性归纳
    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        observations: list[dict] = []
        findings: list[dict] = []

        # 1) 先交代口径与实验背景：同一个数字在不同口径下含义不同
        kb_evidence = [e for e in collected.evidence if e.get("source_kind") != "metric"]
        for item in kb_evidence[:3]:
            content = (item.get("content") or "").strip().replace("\n", " ")[:200]
            if not content:
                continue
            findings.append(
                {
                    "text": f"指标口径/实验背景（《{item.get('source_name')}》）：{content}",
                    "type": "fact",
                    "evidence_refs": [item["local_ref"]],
                    "confidence": 0.6,
                }
            )
        experiment_hits = [
            item
            for item in kb_evidence
            if any(k in (item.get("content") or "") for k in ("实验", "分流", "A/B", "ab test", "灰度"))
        ]
        if experiment_hits:
            findings.append(
                {
                    "text": "存在与本次问题窗口重叠的实验/灰度记录：指标变化不能只归因于版本，"
                    "需要先确认实验分组与版本发布是否正交。",
                    "type": "inference",
                    "evidence_refs": sorted({item["local_ref"] for item in experiment_hits}),
                    "confidence": 0.65,
                }
            )

        # 2) 再解释指标查询结果本身
        for item in collected.metric_results:
            ref = item["local_ref"]
            metric = item.get("metric_name")
            capability = item.get("capability")
            summary = item.get("summary") or {}
            params = item.get("params") or {}
            scope = " ".join(
                f"{k}={params.get(k)}"
                for k in ("platform", "app_version", "channel", "device")
                if params.get(k)
            )
            direction = "unknown"
            magnitude = None
            delta = summary.get("delta")
            if delta is None and summary.get("baseline_avg") is not None:
                delta = summary.get("target_avg", 0) - summary.get("baseline_avg", 0)
            if isinstance(delta, (int, float)):
                magnitude = round(float(delta), 4)
                direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")

            observations.append(
                {
                    "metric_name": metric,
                    "capability": capability,
                    "direction": direction,
                    "magnitude": magnitude,
                    "scope": scope,
                    "dataset_version": item.get("dataset_version"),
                    "warnings": item.get("warnings") or [],
                }
            )

            if capability == "detect_anomaly":
                # 异常检测的 rows 是"异常点"，不是原始序列：没异常点 ≠ 没数据
                points = summary.get("points") or 0
                if not points:
                    findings.append(
                        {
                            "text": f"指标 {metric}（异常检测，范围 {scope or '全量'}）在指定时间窗内"
                            f"没有数据，无法判断该维度的影响。",
                            "type": "fact",
                            "evidence_refs": [ref],
                            "confidence": 0.85,
                        }
                    )
                    continue
                count = summary.get("anomaly_count") or 0
                if count:
                    dates = [row.get("stat_date") for row in (item.get("rows") or [])][:5]
                    findings.append(
                        {
                            "text": f"异常检测（{summary.get('method')}，阈值 {summary.get('threshold')}，"
                            f"{points} 个数据点）发现 {count} 个异常点：{', '.join(str(d) for d in dates)}。",
                            "type": "fact",
                            "evidence_refs": [ref],
                            "confidence": 0.8,
                        }
                    )
                else:
                    findings.append(
                        {
                            "text": f"异常检测未在 {metric} 上发现显著异常点（{points} 个数据点）。",
                            "type": "fact",
                            "evidence_refs": [ref],
                            "confidence": 0.7,
                        }
                    )
                if item.get("quality_flags"):
                    findings.append(
                        {
                            "text": f"数据质量提示：{metric} 数据带质量标记 "
                            f"{'、'.join(str(f) for f in item['quality_flags'])}，结论需谨慎使用。",
                            "type": "fact",
                            "evidence_refs": [ref],
                            "confidence": 0.78,
                        }
                    )
                continue

            if not item.get("rows"):
                findings.append(
                    {
                        "text": f"指标 {metric}（{capability}，范围 {scope or '全量'}）在指定时间窗内"
                        f"没有数据，无法判断该维度的影响。",
                        "type": "fact",
                        "evidence_refs": [ref],
                        "confidence": 0.85,
                    }
                )
                continue

            if capability == "compare_versions":
                base_avg = summary.get("baseline_avg")
                target_avg = summary.get("target_avg")
                pct = summary.get("pct_change")
                if base_avg is not None and target_avg is not None:
                    pct_text = f"，相对变化 {pct:+.1%}" if isinstance(pct, (int, float)) else ""
                    direction_text = _direction_word(target_avg - base_avg)
                    findings.append(
                        {
                            "text": f"版本对比（{metric}）：{summary.get('baseline_version')} 均值 "
                            f"{base_avg:.4f} → {summary.get('target_version')} 均值 {target_avg:.4f}"
                            f"{pct_text}，方向：{direction_text}"
                            f"（范围 {scope or '全量'}，数据版本 {item.get('dataset_version')}）。",
                            "type": "fact",
                            "evidence_refs": [ref],
                            "confidence": 0.9,
                        }
                    )
                if summary.get("release_delta") is not None:
                    delta = summary.get("release_delta")
                    findings.append(
                        {
                            "text": f"发布日切分（{metric}）：发布前均值 "
                            f"{summary.get('pre_release_avg'):.4f} → 发布后均值 "
                            f"{summary.get('post_release_avg'):.4f}，差值 {delta:+.4f}"
                            f"，方向：{_direction_word(delta)}。",
                            "type": "fact",
                            "evidence_refs": [ref],
                            "confidence": 0.88,
                        }
                    )
            elif capability == "get_trend":
                if summary.get("start_value") is not None:
                    start_value = summary.get("start_value")
                    end_value = summary.get("end_value")
                    findings.append(
                        {
                            "text": f"时间趋势（{metric}）：从 {start_value:.4f} "
                            f"{_direction_word(end_value - start_value)}至 {end_value:.4f}"
                            f"（{summary.get('points')} 个数据点，最低 {summary.get('min_value'):.4f}，"
                            f"最高 {summary.get('max_value'):.4f}）。",
                            "type": "fact",
                            "evidence_refs": [ref],
                            "confidence": 0.85,
                        }
                    )
            elif capability == "breakdown":
                if summary.get("top_key"):
                    rows = [row for row in (item.get("rows") or []) if row.get("avg_value") is not None]
                    # 只给"占比最高"是不够的：占比高不等于表现最差，
                    # 而"哪个分群数值最低"才是定位受影响人群的关键信息。
                    extremes = ""
                    if rows:
                        lowest = min(rows, key=lambda row: float(row.get("avg_value") or 0))
                        highest = max(rows, key=lambda row: float(row.get("avg_value") or 0))
                        extremes = (
                            f"；分组均值最低为「{lowest.get('key')}」{float(lowest['avg_value']):.4f}，"
                            f"最高为「{highest.get('key')}」{float(highest['avg_value']):.4f}"
                            f"（建议以均值差异而非占比判断受影响人群）"
                        )
                    findings.append(
                        {
                            "text": f"分群下钻（{metric}）：按 {summary.get('dimension')} 拆分后，"
                            f"「{summary.get('top_key')}」占比最高"
                            f"（{summary.get('top_share'):.2%}，共 {summary.get('groups')} 个分组）{extremes}。",
                            "type": "fact",
                            "evidence_refs": [ref],
                            "confidence": 0.8,
                        }
                    )

            if item.get("quality_flags"):
                findings.append(
                    {
                        "text": f"数据质量提示：{metric} 数据带质量标记 "
                        f"{'、'.join(str(f) for f in item['quality_flags'])}，结论需谨慎使用。",
                        "type": "fact",
                        "evidence_refs": [ref],
                        "confidence": 0.78,
                    }
                )

        if observations:
            dropped = [o for o in observations if o["direction"] == "down"]
            if dropped:
                worst = min(
                    (o for o in dropped if o["magnitude"] is not None),
                    key=lambda o: o["magnitude"] or 0,
                    default=None,
                )
                if worst:
                    findings.append(
                        {
                            "text": f"综合指标趋势：共有 {len(dropped)} 项指标呈下降方向，"
                            f"下降幅度最大的是 {worst['metric_name']}（{worst['scope'] or '全量'}）。",
                            "type": "inference",
                            "evidence_refs": [
                                item["local_ref"]
                                for item in collected.metric_results
                                if item.get("metric_name") == worst["metric_name"]
                            ],
                            "confidence": 0.7,
                        }
                    )

        summary = (
            f"执行 {len(collected.metric_results)} 次受控指标查询，"
            f"得到 {len(findings)} 条指标事实。"
            if collected.metric_results
            else "未能取得任何指标数据。"
        )
        return {
            "summary": summary,
            "findings": findings,
            "observations": observations,
            "data_gaps": list(dict.fromkeys(collected.data_gaps)),
        }

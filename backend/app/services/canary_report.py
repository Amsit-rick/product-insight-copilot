"""灰度效果自动对比（模型 A/B 复盘）。

灰度上线后最难的不是"切流量"，而是**判断候选模型到底好不好**：
- 只看几个任务的观感 → 结论不可靠（噪声 + 主观）；
- 只看成本 → 会把"便宜但质量更差"判成赢；
- 小样本上直接比平均 → 会把随机波动当成结论。

这个模块的做法：按 `agent_runs.output.model_source`（`canary` = 走了候选模型，
其它 = 对照）分组，统计**质量、成本、稳定性**三类指标，并且：

1. **先卡样本量**：任一侧任务数不足 `canary_min_sample` 就直接给"样本不足，暂不结论"，
   不输出任何胜负判断（这是最重要的一条——避免用 3 个任务的差异做决策）；
2. **比率类指标用两比例 z 检验**：p 值不足 0.05 时报"无显著差异"，不报"更优"；
3. **均值类指标用相对差 + 最小可辨差异**：明确标注这是启发式阈值而非统计显著性，
   不假装有统计学效力；
4. **结论必须带证据**：给出各自的样本量、指标值与差异，任何一条结论都能回溯到数字。

统计口径与局限都写在返回的 `method` 字段里，前端与文档直接引用它，避免"数字从哪来"说不清。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.logging_utils import get_logger
from app.models.workflow import AgentRun, AnalysisTask, Report
from app.services.cost import UsageRecord, compute_cost

logger = get_logger(__name__)

CANARY_LABEL = "canary"
CONTROL_LABEL = "control"

# 均值类指标的"最小可辨差异"：低于它就别宣称差异（纯启发式，明确不当作显著性）
MEAN_DELTA_THRESHOLD = 0.10


@dataclass
class GroupStats:
    """一侧（灰度或对照）的聚合结果。"""

    label: str
    task_count: int = 0
    run_count: int = 0
    model_calls: int = 0
    model_used_rate: float = 0.0
    schema_ok_rate: float = 0.0
    error_rate: float = 0.0
    avg_findings: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    total_tokens: int = 0
    total_cost: float = 0.0
    cost_per_task: float = 0.0
    report_count: int = 0
    avg_facts_per_report: float = 0.0
    cited_fact_rate: float = 0.0
    conflict_rate: float = 0.0
    human_review_rate: float = 0.0
    avg_confidence: float = 0.0
    models: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "task_count": self.task_count,
            "run_count": self.run_count,
            "model_calls": self.model_calls,
            "model_used_rate": round(self.model_used_rate, 4),
            "schema_ok_rate": round(self.schema_ok_rate, 4),
            "error_rate": round(self.error_rate, 4),
            "avg_findings": round(self.avg_findings, 2),
            "latency_p50_ms": round(self.latency_p50_ms, 1),
            "latency_p95_ms": round(self.latency_p95_ms, 1),
            "total_tokens": self.total_tokens,
            "total_cost": round(self.total_cost, 6),
            "cost_per_task": round(self.cost_per_task, 6),
            "report_count": self.report_count,
            "avg_facts_per_report": round(self.avg_facts_per_report, 2),
            "cited_fact_rate": round(self.cited_fact_rate, 4),
            "conflict_rate": round(self.conflict_rate, 4),
            "human_review_rate": round(self.human_review_rate, 4),
            "avg_confidence": round(self.avg_confidence, 4),
            "models": sorted(self.models),
        }


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return float(ordered[index])


def _two_proportion_p(success_a: int, total_a: int, success_b: int, total_b: int) -> float | None:
    """两比例 z 检验的双侧 p 值（正态近似）。样本太小时返回 None。"""
    if total_a < 5 or total_b < 5:
        return None
    p_a = success_a / total_a
    p_b = success_b / total_b
    pooled = (success_a + success_b) / (total_a + total_b)
    if pooled in (0.0, 1.0):
        return 1.0
    se = math.sqrt(pooled * (1 - pooled) * (1 / total_a + 1 / total_b))
    if se == 0:
        return 1.0
    z = abs(p_a - p_b) / se
    # 双侧：p = 2 * (1 - Φ(|z|))，用 erf 实现标准正态 CDF
    return max(0.0, min(1.0, 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))))


def _group_of(run: AgentRun) -> str:
    source = str(((run.output or {}).get("model_source") or "")).lower()
    return CANARY_LABEL if source == "canary" else CONTROL_LABEL


def _is_model_call(run: AgentRun) -> bool:
    """是否真的调用了模型（stub 降级的运行不计入模型质量指标）。"""
    output = run.output or {}
    return bool(output.get("model_used"))


def collect_groups(
    session: Session,
    tenant_id: str,
    *,
    days: int = 14,
    agent_code: str | None = None,
    settings: Settings | None = None,
) -> dict[str, GroupStats]:
    """按 model_source 分组聚合。返回 {canary: GroupStats, control: GroupStats}。"""
    settings = settings or get_settings()
    since = datetime.utcnow() - timedelta(days=days)
    runs = (
        session.query(AgentRun)
        .join(AnalysisTask, AnalysisTask.id == AgentRun.task_id)
        .filter(AnalysisTask.tenant_id == tenant_id)
        .filter(AnalysisTask.created_at >= since)
        .filter(AgentRun.agent_code == agent_code if agent_code else True)
        .all()
    )
    task_ids = {run.task_id for run in runs}

    reports = (
        session.query(Report)
        .filter(Report.task_id.in_(task_ids or [""]))
        .order_by(Report.version.desc())
        .all()
    ) if task_ids else []
    latest_report: dict[str, Report] = {}
    for report in reports:
        latest_report.setdefault(report.task_id, report)  # 已按 version desc，首条即最新

    tasks_by_group: dict[str, set[str]] = {CANARY_LABEL: set(), CONTROL_LABEL: set()}
    for run in runs:
        tasks_by_group[_group_of(run)].add(run.task_id)

    groups: dict[str, GroupStats] = {}
    for label in (CANARY_LABEL, CONTROL_LABEL):
        group_runs = [run for run in runs if _group_of(run) == label]
        stats = GroupStats(label=label)
        stats.run_count = len(group_runs)
        stats.task_count = len(tasks_by_group[label])
        stats.model_calls = sum(1 for run in group_runs if _is_model_call(run))
        stats.model_used_rate = stats.model_calls / stats.run_count if stats.run_count else 0.0
        stats.schema_ok_rate = (
            sum(1 for run in group_runs if (run.output or {}).get("schema_ok")) / stats.run_count
            if stats.run_count
            else 0.0
        )
        stats.error_rate = (
            sum(1 for run in group_runs if run.status == "failed" or (run.output or {}).get("model_error"))
            / stats.run_count
            if stats.run_count
            else 0.0
        )
        findings = [len((run.output or {}).get("findings") or []) for run in group_runs]
        stats.avg_findings = statistics.fmean(findings) if findings else 0.0
        latencies = [float(run.latency_ms or 0) for run in group_runs if run.latency_ms]
        stats.latency_p50_ms = _percentile(latencies, 0.5)
        stats.latency_p95_ms = _percentile(latencies, 0.95)
        stats.total_tokens = sum(int(run.total_tokens or 0) for run in group_runs)
        stats.models = {str(run.model) for run in group_runs if run.model}

        records = [
            {
                "model": run.model or "stub",
                "input_tokens": int(run.prompt_tokens or 0),
                "output_tokens": int(run.completion_tokens or 0),
                "cache_hit_tokens": int((run.output or {}).get("cache_hit_tokens") or 0),
            }
            for run in group_runs
            if _is_model_call(run) or int(run.total_tokens or 0) > 0
        ]
        breakdown = compute_cost(
            [
                UsageRecord(
                    model=item["model"],
                    input_tokens=item["input_tokens"],
                    output_tokens=item["output_tokens"],
                    cache_hit_tokens=item["cache_hit_tokens"],
                )
                for item in records
            ],
            settings,
        )
        stats.total_cost = breakdown.amount
        stats.cost_per_task = breakdown.amount / stats.task_count if stats.task_count else 0.0

        group_reports = [
            latest_report[task_id] for task_id in tasks_by_group[label] if task_id in latest_report
        ]
        stats.report_count = len(group_reports)
        if group_reports:
            facts = [len(report.facts or []) for report in group_reports]
            stats.avg_facts_per_report = statistics.fmean(facts)
            cited = 0
            total_facts = 0
            for report in group_reports:
                for fact in report.facts or []:
                    total_facts += 1
                    if fact.get("evidence_refs"):
                        cited += 1
            stats.cited_fact_rate = cited / total_facts if total_facts else 0.0
            stats.conflict_rate = (
                sum(1 for report in group_reports if (report.conflicts or [])) / len(group_reports)
            )
            stats.human_review_rate = (
                sum(1 for report in group_reports if report.needs_human_review) / len(group_reports)
            )
            confidences = [float(report.confidence) for report in group_reports if report.confidence]
            stats.avg_confidence = statistics.fmean(confidences) if confidences else 0.0
        groups[label] = stats
    return groups


def _rate_verdict(
    name: str,
    canary_value: float,
    control_value: float,
    canary_hits: int,
    control_hits: int,
    canary_total: int,
    control_total: int,
    *,
    higher_is_better: bool,
    sample_ok: bool = True,
) -> dict[str, Any]:
    p_value = _two_proportion_p(canary_hits, canary_total, control_hits, control_total)
    delta = canary_value - control_value
    if not sample_ok:
        # 任务样本未达门槛：即使比率看起来差别很大也不下结论（p 值仍返回，供人工判断）
        verdict, note = "样本不足", "两侧任务数未达门槛，不对该指标下结论"
    elif p_value is None:
        verdict, note = "样本不足", "任一侧样本 < 5，不做显著性判断"
    elif p_value >= 0.05:
        verdict, note = "无显著差异", f"p={p_value:.3f} ≥ 0.05"
    else:
        better = (delta > 0) if higher_is_better else (delta < 0)
        verdict, note = ("灰度更优" if better else "灰度更差"), f"p={p_value:.3f} < 0.05"
    return {
        "metric": name,
        "canary": round(canary_value, 4),
        "control": round(control_value, 4),
        "delta": round(delta, 4),
        # 比率类指标用"百分点"表达差异：对照侧为 0 时相对百分比没有意义（会显示成 +0.0%）
        "delta_pp": round(delta * 100, 1),
        "delta_pct": round((delta / control_value * 100) if control_value else 0.0, 1),
        "test": "two-proportion-z",
        "p_value": round(p_value, 4) if p_value is not None else None,
        "verdict": verdict,
        "note": note,
    }


def _mean_verdict(
    name: str,
    canary_value: float,
    control_value: float,
    *,
    higher_is_better: bool,
    sample_ok: bool = True,
) -> dict[str, Any]:
    delta = canary_value - control_value
    relative = (delta / control_value) if control_value else 0.0
    if not sample_ok:
        verdict, note = "样本不足", "两侧任务数未达门槛，不对该指标下结论"
    elif abs(relative) < MEAN_DELTA_THRESHOLD:
        verdict, note = "无显著差异", f"相对差 {relative * 100:.1f}% 小于可辨阈值 10%（启发式）"
    else:
        better = (delta > 0) if higher_is_better else (delta < 0)
        verdict, note = ("灰度更优" if better else "灰度更差"), f"相对差 {relative * 100:.1f}%（启发式阈值）"
    return {
        "metric": name,
        "canary": round(canary_value, 4),
        "control": round(control_value, 4),
        "delta": round(delta, 4),
        "delta_pct": round(relative * 100, 1),
        "test": "relative-delta",
        "p_value": None,
        "verdict": verdict,
        "note": note,
    }


def compare(
    session: Session,
    tenant_id: str,
    *,
    days: int = 14,
    agent_code: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """灰度 vs 对照的完整对比（含样本量校验与逐指标结论）。"""
    settings = settings or get_settings()
    groups = collect_groups(session, tenant_id, days=days, agent_code=agent_code, settings=settings)
    canary, control = groups[CANARY_LABEL], groups[CONTROL_LABEL]
    min_sample = int(getattr(settings, "canary_min_sample", 5))
    configured = bool(settings.model_canary)
    # 样本门槛先算出来并贯穿所有指标：样本不足时**任何指标都不给胜负**，
    # 否则会出现"结论栏写着样本不足，表格里却逐项写着灰度更优"的自相矛盾。
    enough = canary.task_count >= min_sample and control.task_count >= min_sample

    metrics: list[dict[str, Any]] = []
    if canary.run_count or control.run_count:
        metrics.append(
            _rate_verdict(
                "model_used_rate",
                canary.model_used_rate,
                control.model_used_rate,
                canary.model_calls,
                control.model_calls,
                canary.run_count,
                control.run_count,
                higher_is_better=True,
                sample_ok=enough,
            )
        )
        metrics.append(
            _rate_verdict(
                "schema_ok_rate",
                canary.schema_ok_rate,
                control.schema_ok_rate,
                round(canary.schema_ok_rate * canary.run_count),
                round(control.schema_ok_rate * control.run_count),
                canary.run_count,
                control.run_count,
                higher_is_better=True,
                sample_ok=enough,
            )
        )
        metrics.append(
            _rate_verdict(
                "error_rate",
                canary.error_rate,
                control.error_rate,
                round(canary.error_rate * canary.run_count),
                round(control.error_rate * control.run_count),
                canary.run_count,
                control.run_count,
                higher_is_better=False,
                sample_ok=enough,
            )
        )
        metrics.append(
            _mean_verdict(
                "avg_findings", canary.avg_findings, control.avg_findings,
                higher_is_better=True, sample_ok=enough,
            )
        )
        metrics.append(
            _mean_verdict(
                "latency_p95_ms", canary.latency_p95_ms, control.latency_p95_ms,
                higher_is_better=False, sample_ok=enough,
            )
        )

    report_metrics: list[dict[str, Any]] = []
    if canary.report_count and control.report_count:
        report_metrics.append(
            _mean_verdict(
                "avg_facts_per_report", canary.avg_facts_per_report, control.avg_facts_per_report,
                higher_is_better=True, sample_ok=enough,
            )
        )
        report_metrics.append(
            _rate_verdict(
                "cited_fact_rate",
                canary.cited_fact_rate,
                control.cited_fact_rate,
                round(canary.cited_fact_rate * canary.report_count),
                round(control.cited_fact_rate * control.report_count),
                canary.report_count,
                control.report_count,
                higher_is_better=True,
                sample_ok=enough,
            )
        )
        report_metrics.append(
            _rate_verdict(
                "human_review_rate",
                canary.human_review_rate,
                control.human_review_rate,
                round(canary.human_review_rate * canary.report_count),
                round(control.human_review_rate * control.report_count),
                canary.report_count,
                control.report_count,
                higher_is_better=False,
                sample_ok=enough,
            )
        )
        report_metrics.append(
            _mean_verdict(
                "avg_confidence", canary.avg_confidence, control.avg_confidence,
                higher_is_better=True, sample_ok=enough,
            )
        )

    # 样本量校验：不足就明说，不产出胜负。
    # 注意顺序：**先看样本、再看配置**——灰度跑完把配置撤掉后，历史数据仍然要能读出结论，
    # 否则"实验一结束就查不到结果"，等于白跑。配置只用来解释"为什么没有样本"。
    if canary.task_count == 0 and control.task_count == 0:
        verdict = "样本不足"
        reason = "窗口内没有任务"
        if not configured:
            reason += "；且当前未配置 MODEL_CANARY（灰度未开启）"
    elif canary.task_count == 0:
        verdict, reason = "样本不足", f"灰度侧还没有任务（对照侧 {control.task_count} 个）"
    elif control.task_count == 0:
        verdict, reason = "样本不足", f"对照侧还没有任务（灰度侧 {canary.task_count} 个）"
    elif not enough:
        verdict, reason = "样本不足", (
            f"两侧任务数需各 ≥ {min_sample}，当前 灰度 {canary.task_count} / 对照 {control.task_count}"
        )
    else:
        wins = sum(1 for item in [*metrics, *report_metrics] if item["verdict"] == "灰度更优")
        losses = sum(1 for item in [*metrics, *report_metrics] if item["verdict"] == "灰度更差")
        if wins > losses:
            verdict, reason = "灰度更优", f"显著更优 {wins} 项、更差 {losses} 项（样本已达标）"
        elif losses > wins:
            verdict, reason = "灰度更差", f"显著更优 {wins} 项、更差 {losses} 项（样本已达标）"
        else:
            verdict, reason = "暂无明显差异", f"更优 {wins} 项、更差 {losses} 项，其余无显著差异"
        if not configured:
            reason += "；注意当前已未配置 MODEL_CANARY，这是对历史流量的复盘"

    return {
        "tenant_id": tenant_id,
        "window_days": days,
        "agent_code": agent_code,
        "canary_configured": configured,
        "canary_config": getattr(settings, "model_canary", {}) or {},
        "min_sample": min_sample,
        "groups": {label: stats.as_dict() for label, stats in groups.items()},
        "metrics": metrics,
        "report_metrics": report_metrics,
        "verdict": verdict,
        "verdict_reason": reason,
        "method": {
            "grouping": "agent_runs.output.model_source == 'canary' 为灰度侧，其余为对照侧",
            "rates": "两比例 z 检验（正态近似），p < 0.05 才判定差异；样本 < 5 直接给'样本不足'",
            "means": f"相对差 ≥ {int(MEAN_DELTA_THRESHOLD * 100)}% 才算差异（启发式阈值，非统计显著性）",
            "limitations": [
                "分组属于「事后归因」：同一任务内的多个 Agent 必然在同一侧（任务级分桶），但它不是随机对照试验",
                "样本量与任务复杂度都会影响质量指标，样本不足时不要下结论",
                "质量指标只覆盖「有事实、有引用、是否需要人工」这类可自动判定的维度，"
                "不包含人工评审的主观打分——真正的选型结论应结合评审",
            ],
        },
    }


def canary_status(settings: Settings | None = None) -> dict[str, Any]:
    """灰度配置概览（供 /health 与实验接口复用，不含密钥）。"""
    settings = settings or get_settings()
    rows = []
    for key, raw in (settings.model_canary or {}).items():
        if not isinstance(raw, dict):
            continue
        rows.append(
            {
                "key": key,
                "model": str(raw.get("model") or settings.llm_model),
                "percent": raw.get("percent", 0),
                "salt": settings.model_canary_salt,
            }
        )
    return {"configured": bool(rows), "targets": rows, "min_sample": settings.canary_min_sample}


__all__ = [
    "CANARY_LABEL",
    "CONTROL_LABEL",
    "GroupStats",
    "canary_status",
    "collect_groups",
    "compare",
]

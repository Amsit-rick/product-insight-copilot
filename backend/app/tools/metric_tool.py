"""指标工具：Agent 只能调用固定能力，永远拿不到 SQL 执行权。"""

from __future__ import annotations

from app.metrics.schemas import (
    AnomalyRequest,
    BreakdownRequest,
    TrendRequest,
    VersionCompareRequest,
)
from app.metrics.service import SqlMetricService
from app.tools.registry import ReplayPolicy, SideEffectLevel, ToolContext, ToolSpec

CAPABILITIES = {
    "get_trend": TrendRequest,
    "compare_versions": VersionCompareRequest,
    "breakdown": BreakdownRequest,
    "detect_anomaly": AnomalyRequest,
}

FORBIDDEN_KEYS = {"sql", "raw_sql", "query", "statement", "table"}


class MetricQueryTool:
    """受控指标查询：四个固定能力 + Pydantic 参数校验。"""

    name = "metric.query"

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                "查询标准化指标。capability 只能是 get_trend / compare_versions / "
                "breakdown / detect_anomaly；不接受任何 SQL 文本。"
            ),
            handler=self.run,
            replay_policy=ReplayPolicy.SAFE,
            side_effect_level=SideEffectLevel.READ_ONLY,
            arg_schema={
                "capability": "get_trend | compare_versions | breakdown | detect_anomaly",
                "params": {
                    "metric_name": "指标名，例如 next_day_retention",
                    "start_date": "YYYY-MM-DD",
                    "end_date": "YYYY-MM-DD",
                    "platform / app_version / channel / device": "可选维度过滤",
                    "baseline_version / target_version": "版本对比必填",
                    "dimension": "breakdown 维度：platform|channel|device|app_version",
                    "method / threshold": "异常检测方法与阈值",
                },
            },
        )

    def run(
        self,
        *,
        session,
        ctx: ToolContext,
        capability: str,
        params: dict | None = None,
        **_ignored,
    ) -> dict:
        raw = dict(params or {})
        # 二次防御：即使上层校验被绕过，也拒绝任何 SQL 形态的参数
        for key in list(raw):
            if key.lower() in FORBIDDEN_KEYS:
                raise ValueError(f"指标工具不接受字段 {key}（禁止任意 SQL）")
        model = CAPABILITIES.get(capability)
        if model is None:
            raise ValueError(f"非法指标能力: {capability}")

        request = model.model_validate(raw)
        service = SqlMetricService(
            session,
            tenant_id=ctx.tenant_id,
            roles=list(ctx.roles),
            user_id=ctx.user_id,
            clearance=ctx.max_security_rank,
        )
        if capability == "get_trend":
            result = service.get_trend(request)
        elif capability == "compare_versions":
            result = service.compare_versions(request)
        elif capability == "breakdown":
            result = service.breakdown(request)
        else:
            result = service.detect_anomaly(request)

        denied = bool((result.summary or {}).get("policy_denied"))
        return {
            "capability": capability,
            "metric_name": result.metric_name,
            "rows": result.rows,
            "summary": result.summary,
            "available": result.available,
            # 密级拒绝：如实告知 Agent"无权访问"，而不是让它以为"这个指标没数据"——
            # 前者会写进报告的不确定性说明，后者会变成一句错误的"数据缺失"结论
            "policy_denied": denied,
            "denied_reason": (result.summary or {}).get("reason") if denied else None,
            "dataset_version": result.dataset_version,
            "source_version": result.source_version,
            "quality_flags": result.quality_flags,
            "warnings": result.warnings,
            "params": result.params,
        }


def metric_query(**kwargs) -> dict:
    return MetricQueryTool().run(**kwargs)

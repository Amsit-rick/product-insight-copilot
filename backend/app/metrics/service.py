"""MetricService：受控指标查询（设计文档 8.3 节）。

只暴露四个固定能力，全部通过 ORM 参数化查询实现，
不拼接任何来自用户输入或直接来自模型的 SQL。
"""

from __future__ import annotations

import hashlib
import statistics
from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_utils import get_logger
from app.metrics.schemas import (
    AnomalyRequest,
    BreakdownRequest,
    MetricResult,
    TrendRequest,
    VersionCompareRequest,
)
from app.models.metrics import MetricDefinition, MetricSnapshot

if TYPE_CHECKING:  # 仅用于类型标注，避免运行时循环导入
    from app.services.metric_access import MetricScope

logger = get_logger(__name__)


class MetricService(Protocol):
    def get_trend(self, request: TrendRequest) -> MetricResult: ...
    def compare_versions(self, request: VersionCompareRequest) -> MetricResult: ...
    def breakdown(self, request: BreakdownRequest) -> MetricResult: ...
    def detect_anomaly(self, request: AnomalyRequest) -> MetricResult: ...


class SqlMetricService:
    """基于标准化指标表（metric_snapshots）的实现。

    访问范围（`roles` / `user_id` / `clearance`）是**必传的上下文**：
    它既用来做指标级密级判定，也参与缓存键——否则 A 角色查到的结果会被 B 角色命中，
    这在"指标有密级"的前提下就是数据泄露。
    """

    def __init__(
        self,
        session: Session,
        tenant_id: str = "*",
        *,
        roles: list[str] | None = None,
        user_id: str | None = None,
        clearance: int | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.roles = list(roles or [])
        self.user_id = user_id
        self.clearance = clearance
        self.settings = get_settings()
        # 单次服务实例内的策略判定缓存（同一次查询里同一指标只查一次策略表）
        self._scope_cache: dict[tuple, MetricScope] = {}

    # ------------------------------------------------------------------ 访问控制
    def scope_for(self, metric_name: str) -> "MetricScope":
        """该指标的访问范围（是否允许 + 行级数据范围）。

        结果按 (指标, 角色, 用户, 密级) 缓存一次，避免同一次查询里反复查策略表。
        """
        from app.services.metric_access import resolve_metric_scope

        cache_key = (metric_name, tuple(sorted(self.roles)), self.user_id, self.clearance)
        if cache_key in self._scope_cache:
            return self._scope_cache[cache_key]
        scope = resolve_metric_scope(
            self.session,
            tenant_id=self.tenant_id,
            roles=self.roles,
            user_id=self.user_id,
            clearance=self.clearance,
            metric_name=metric_name,
        )
        self._scope_cache[cache_key] = scope
        return scope

    def _denied(self, capability: str, request) -> MetricResult | None:
        """指标级密级判定：被拒时返回"空结果 + 明确原因"，绝不返回任何数值。"""
        from app.services.metric_access import check_dimension_values

        scope = self.scope_for(request.metric_name)
        if not scope.allowed:
            return self._policy_result(capability, request, scope.reason, filtered=False)
        # 请求里显式指定的维度值越出数据范围：同样按"无权访问"处理，
        # 不能让用户以为"这个渠道没数据"
        violation = check_dimension_values(scope, request.model_dump(mode="json"))
        if violation:
            return self._policy_result(capability, request, violation, filtered=True)
        return None

    def _policy_result(self, capability: str, request, reason: str, *, filtered: bool) -> MetricResult:
        return MetricResult(
            capability=capability,
            metric_name=request.metric_name,
            params=request.model_dump(mode="json"),
            rows=[],
            summary={
                "policy_denied": True,
                "reason": reason,
                "available": False,
                "data_range_denied": filtered,
            },
            warnings=[
                f"无权访问指标 {request.metric_name}：{reason}"
                if not filtered
                else f"指标 {request.metric_name} 的本次请求超出你的数据范围：{reason}"
            ],
        )

    def _apply_row_filters(self, query, scope, metric_name: str):
        """把行级数据范围加进 SQL（在数据库层过滤，不依赖上层自觉）。"""
        for dimension, values in (scope.row_filters or {}).items():
            column = getattr(MetricSnapshot, dimension)
            query = query.filter(column.in_(values))
        return query

    @property
    def access_scope_key(self) -> str:
        """访问范围指纹：进缓存键，避免不同角色之间互相命中。"""
        if not self.roles and not self.user_id and self.clearance is None:
            return "open"
        raw = f"{sorted(self.roles)}|{self.user_id or ''}|{self.clearance or ''}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ------------------------------------------------------------------ 基础查询
    def _base_query(self, metric_name: str, start: date, end: date, **dims: str | None):
        query = (
            self.session.query(MetricSnapshot)
            .filter(MetricSnapshot.tenant_id == self.tenant_id)
            .filter(MetricSnapshot.metric_name == metric_name)
            .filter(MetricSnapshot.stat_date >= start)
            .filter(MetricSnapshot.stat_date <= end)
        )
        for column in ("platform", "app_version", "channel", "device"):
            value = dims.get(column)
            if value:
                query = query.filter(getattr(MetricSnapshot, column) == value)
        return query

    def _row(self, snap: MetricSnapshot) -> dict:
        return {
            "metric_name": snap.metric_name,
            "stat_date": snap.stat_date.isoformat(),
            "platform": snap.platform,
            "app_version": snap.app_version,
            "channel": snap.channel,
            "device": snap.device,
            "value": snap.value,
            "sample_size": snap.sample_size,
            "dataset_version": snap.dataset_version,
            "quality_flags": snap.quality_flags or [],
        }

    def _versions(self, rows: list[MetricSnapshot]) -> tuple[str | None, str | None]:
        dsv = {r.dataset_version for r in rows if r.dataset_version}
        src = {r.source_version for r in rows if r.source_version}
        return (sorted(dsv)[-1] if dsv else None, sorted(src)[-1] if src else None)

    def _quality_flags(self, rows: list[MetricSnapshot]) -> list[str]:
        flags: set[str] = set()
        for row in rows:
            flags.update(row.quality_flags or [])
        return sorted(flags)

    def _definition(self, metric_name: str) -> MetricDefinition | None:
        return (
            self.session.query(MetricDefinition)
            .filter(MetricDefinition.name == metric_name)
            .first()
        )

    # ------------------------------------------------------------ 缓存包装
    def _cached(self, capability: str, request, producer) -> MetricResult:
        """指标查询缓存。

        key 里带指标数据指纹（导入批次数 / 最近导入时间 / 快照行数）与**访问范围指纹**：
        前者保证重新导入后旧缓存自动失效，后者保证不同角色不会命中彼此的结果
        （指标一旦有密级，缓存键里就必须有角色，否则就是越权读取的捷径）。
        模型与 Agent 都感知不到这层缓存。
        """
        from app.services.caching import cached_call, make_key, metric_fingerprint

        ttl = getattr(self.settings, "metric_cache_ttl_seconds", 0)
        denied = self._denied(capability, request)
        if denied is not None:
            return denied  # 无权访问的结果不缓存：避免角色授权变化后被旧缓存挡住
        scope = self.scope_for(request.metric_name)
        if ttl <= 0:
            return producer()
        key = make_key(
            "metric",
            {
                "tenant": self.tenant_id,
                "capability": capability,
                "params": request.model_dump(mode="json"),
                "scope": self.access_scope_key,
                # 数据范围指纹：策略放宽/收紧后旧缓存必须失效，
                # 否则会把"现在已无权看到的数据"从缓存里发出去
                "range": scope.cache_signature,
                "fp": metric_fingerprint(self.session, self.tenant_id),
            },
        )
        payload, _cached = cached_call(key, ttl, lambda: producer().as_dict(), namespace="metric")
        return MetricResult.from_dict(payload)

    # ------------------------------------------------------------ 能力 1：时间趋势
    def get_trend(self, request: TrendRequest) -> MetricResult:
        return self._cached("get_trend", request, lambda: self._get_trend(request))

    def _get_trend(self, request: TrendRequest) -> MetricResult:
        scope = self.scope_for(request.metric_name)
        rows = (
            self._apply_row_filters(
                self._base_query(
                    request.metric_name,
                    request.start_date,
                    request.end_date,
                    platform=request.platform,
                    app_version=request.app_version,
                    channel=request.channel,
                    device=request.device,
                ),
                scope,
                request.metric_name,
            )
            .order_by(MetricSnapshot.stat_date)
            .all()
        )
        # 同一天多维度的取值按样本量加权平均，口径统一为日粒度
        by_date: dict[str, list[MetricSnapshot]] = defaultdict(list)
        for row in rows:
            by_date[row.stat_date.isoformat()].append(row)

        series = []
        for day in sorted(by_date):
            group = by_date[day]
            total_weight = sum((g.sample_size or 1) for g in group)
            value = sum(g.value * (g.sample_size or 1) for g in group) / max(1, total_weight)
            series.append({"stat_date": day, "value": round(value, 6), "points": len(group)})

        summary: dict = {"points": len(series)}
        warnings: list[str] = []
        if series:
            first, last = series[0]["value"], series[-1]["value"]
            delta = last - first
            summary.update(
                {
                    "start_value": first,
                    "end_value": last,
                    "delta": round(delta, 6),
                    "pct_change": round(delta / first, 6) if first else None,
                    "min_value": min(s["value"] for s in series),
                    "max_value": max(s["value"] for s in series),
                    "avg_value": round(sum(s["value"] for s in series) / len(series), 6),
                }
            )
        else:
            warnings.append("指定条件下没有指标数据（数据缺失）")
        if scope.filtered:
            warnings.append(scope.note)

        dataset_version, source_version = self._versions(rows)
        return MetricResult(
            capability="get_trend",
            metric_name=request.metric_name,
            params=request.model_dump(mode="json"),
            rows=series,
            summary=summary,
            dataset_version=dataset_version,
            source_version=source_version,
            quality_flags=self._quality_flags(rows),
            warnings=warnings,
        )

    # ------------------------------------------------------------ 能力 2：版本对比
    def compare_versions(self, request: VersionCompareRequest) -> MetricResult:
        return self._cached(
            "compare_versions", request, lambda: self._compare_versions(request)
        )

    def _compare_versions(self, request: VersionCompareRequest) -> MetricResult:
        scope = self.scope_for(request.metric_name)

        def _series(version: str) -> list[dict]:
            rows = (
                self._apply_row_filters(
                    self._base_query(
                        request.metric_name,
                        request.start_date,
                        request.end_date,
                        platform=request.platform,
                        app_version=version,
                        channel=request.channel,
                    ),
                    scope,
                    request.metric_name,
                )
                .order_by(MetricSnapshot.stat_date)
                .all()
            )
            return [
                {"stat_date": r.stat_date.isoformat(), "value": r.value, "app_version": version}
                for r in rows
            ]

        baseline = _series(request.baseline_version)
        target = _series(request.target_version)
        all_rows = baseline + target

        def _avg(series: list[dict]) -> float | None:
            return round(sum(r["value"] for r in series) / len(series), 6) if series else None

        base_avg, target_avg = _avg(baseline), _avg(target)
        summary = {
            "baseline_version": request.baseline_version,
            "target_version": request.target_version,
            "baseline_avg": base_avg,
            "target_avg": target_avg,
            "baseline_points": len(baseline),
            "target_points": len(target),
        }
        warnings: list[str] = []
        if base_avg is not None and target_avg is not None and base_avg:
            delta = target_avg - base_avg
            summary["delta"] = round(delta, 6)
            summary["pct_change"] = round(delta / base_avg, 6)
        else:
            warnings.append("版本对比数据不完整，无法给出可靠差值")

        # 若提供发布日，额外给出发布前后对比
        if request.release_date:
            before = [r["value"] for r in target if r["stat_date"] < request.release_date.isoformat()]
            after = [r["value"] for r in target if r["stat_date"] >= request.release_date.isoformat()]
            if before and after:
                summary["pre_release_avg"] = round(sum(before) / len(before), 6)
                summary["post_release_avg"] = round(sum(after) / len(after), 6)
                summary["release_delta"] = round(
                    summary["post_release_avg"] - summary["pre_release_avg"], 6
                )

        dataset_version, source_version = self._versions(
            self._base_query(
                request.metric_name,
                request.start_date,
                request.end_date,
                platform=request.platform,
                channel=request.channel,
            ).all()
        )
        return MetricResult(
            capability="compare_versions",
            metric_name=request.metric_name,
            params=request.model_dump(mode="json"),
            rows=all_rows,
            summary=summary,
            dataset_version=dataset_version,
            source_version=source_version,
            quality_flags=[],
            warnings=warnings,
        )

    # ------------------------------------------------------------ 能力 3：维度分群
    def breakdown(self, request: BreakdownRequest) -> MetricResult:
        return self._cached("breakdown", request, lambda: self._breakdown(request))

    def _breakdown(self, request: BreakdownRequest) -> MetricResult:
        scope = self.scope_for(request.metric_name)
        rows = self._apply_row_filters(
            self._base_query(
                request.metric_name,
                request.start_date,
                request.end_date,
                platform=request.platform,
                app_version=request.app_version,
                channel=request.channel,
            ),
            scope,
            request.metric_name,
        ).all()

        groups: dict[str, list[MetricSnapshot]] = defaultdict(list)
        for row in rows:
            key = str(getattr(row, request.dimension) or "unknown")
            groups[key].append(row)

        total = sum(r.value for r in rows) or 1.0
        buckets = []
        for key, group in groups.items():
            avg = sum(g.value for g in group) / len(group)
            bucket_sum = sum(g.value for g in group)
            buckets.append(
                {
                    "dimension": request.dimension,
                    "key": key,
                    "avg_value": round(avg, 6),
                    "sum_value": round(bucket_sum, 6),
                    "share": round(bucket_sum / total, 6),
                    "points": len(group),
                }
            )
        buckets.sort(key=lambda b: b["sum_value"], reverse=True)
        top = buckets[: request.top_n]

        summary = {
            "dimension": request.dimension,
            "groups": len(buckets),
            "top_key": top[0]["key"] if top else None,
            "top_share": top[0]["share"] if top else None,
            "total_points": len(rows),
        }
        warnings = [] if rows else ["没有可分解的指标数据"]
        if scope.filtered:
            warnings.append(scope.note)
        dataset_version, source_version = self._versions(rows)
        return MetricResult(
            capability="breakdown",
            metric_name=request.metric_name,
            params=request.model_dump(mode="json"),
            rows=top,
            summary=summary,
            dataset_version=dataset_version,
            source_version=source_version,
            quality_flags=self._quality_flags(rows),
            warnings=warnings,
        )

    # ------------------------------------------------------------ 能力 4：异常识别
    def detect_anomaly(self, request: AnomalyRequest) -> MetricResult:
        return self._cached("detect_anomaly", request, lambda: self._detect_anomaly(request))

    def _detect_anomaly(self, request: AnomalyRequest) -> MetricResult:
        trend = self.get_trend(
            TrendRequest(
                metric_name=request.metric_name,
                start_date=request.start_date,
                end_date=request.end_date,
                platform=request.platform,
                app_version=request.app_version,
                channel=request.channel,
            )
        )
        values = [r["value"] for r in trend.rows]
        anomalies: list[dict] = []
        if len(values) >= 3:
            if request.method == "zscore":
                mean = statistics.fmean(values)
                stdev = statistics.pstdev(values) or 1e-9
                for row in trend.rows:
                    z = (row["value"] - mean) / stdev
                    if abs(z) >= request.threshold:
                        anomalies.append(
                            {
                                "stat_date": row["stat_date"],
                                "value": row["value"],
                                "zscore": round(z, 4),
                                "direction": "drop" if z < 0 else "spike",
                            }
                        )
            else:
                ordered = sorted(values)
                q1 = ordered[len(ordered) // 4]
                q3 = ordered[(3 * len(ordered)) // 4]
                iqr = (q3 - q1) or 1e-9
                low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                for row in trend.rows:
                    if row["value"] < low or row["value"] > high:
                        anomalies.append(
                            {
                                "stat_date": row["stat_date"],
                                "value": row["value"],
                                "direction": "drop" if row["value"] < low else "spike",
                                "bound": [round(low, 6), round(high, 6)],
                            }
                        )

        summary = {
            "method": request.method,
            "threshold": request.threshold,
            "points": len(values),
            "anomaly_count": len(anomalies),
            "first_anomaly": anomalies[0]["stat_date"] if anomalies else None,
            "mean": round(statistics.fmean(values), 6) if values else None,
        }
        return MetricResult(
            capability="detect_anomaly",
            metric_name=request.metric_name,
            params=request.model_dump(mode="json"),
            rows=anomalies,
            summary=summary,
            dataset_version=trend.dataset_version,
            source_version=trend.source_version,
            quality_flags=trend.quality_flags,
            warnings=trend.warnings,
        )

    # ------------------------------------------------------------------ 辅助能力
    def visible_metric_names(self, names: list[str]) -> list[str]:
        """按指标级密级过滤清单（定义、覆盖度、版本列表都要走这里，避免"清单泄露"）。"""
        from app.services.metric_access import visible_metrics

        return visible_metrics(
            self.session,
            tenant_id=self.tenant_id,
            roles=self.roles,
            user_id=self.user_id,
            clearance=self.clearance,
            metric_names=list(names),
        )

    def list_metrics(self) -> list[dict]:
        rows = (
            self.session.query(MetricDefinition)
            .filter(MetricDefinition.is_active.is_(True))
            .order_by(MetricDefinition.name)
            .all()
        )
        visible = set(self.visible_metric_names([row.name for row in rows]))
        return [
            {
                "name": r.name,
                "display_name": r.display_name,
                "unit": r.unit,
                "period": r.period,
                "allowed_dimensions": r.allowed_dimensions,
                "description": r.description,
                "higher_is_better": r.higher_is_better,
            }
            for r in rows
            if r.name in visible
        ]

    def coverage(self, metric_name: str) -> dict:
        scope = self.scope_for(metric_name)
        if not scope.allowed:
            # 覆盖度也能暴露"这个指标存在且有多少数据"，因此同样按密级处理
            return {
                "metric_name": metric_name,
                "min_date": None,
                "max_date": None,
                "rows": 0,
                "policy_denied": True,
                "reason": scope.reason,
            }
        query = (
            self.session.query(
                func.min(MetricSnapshot.stat_date),
                func.max(MetricSnapshot.stat_date),
                func.count(MetricSnapshot.id),
            )
            .filter(MetricSnapshot.tenant_id == self.tenant_id)
            .filter(MetricSnapshot.metric_name == metric_name)
        )
        # 覆盖度同样要按数据范围统计：否则行数会泄露"你无权看的那些行"存在多少
        query = self._apply_row_filters(query, scope, metric_name)
        row = query.first()
        payload = {
            "metric_name": metric_name,
            "min_date": row[0].isoformat() if row and row[0] else None,
            "max_date": row[1].isoformat() if row and row[1] else None,
            "rows": row[2] if row else 0,
        }
        if scope.filtered:
            payload["data_range_note"] = scope.note
        return payload

    def list_versions(
        self,
        metric_name: str,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict]:
        """列出某指标在时间窗内出现过的版本及其覆盖情况。

        用途：计划节点据此挑选对比基线版本，避免让 Agent "猜"版本号。
        无权访问的指标直接返回空列表——版本分布本身也是信息。
        """
        scope = self.scope_for(metric_name)
        if not scope.allowed:
            return []
        query = (
            self.session.query(
                MetricSnapshot.app_version,
                func.count(MetricSnapshot.id),
                func.min(MetricSnapshot.stat_date),
                func.max(MetricSnapshot.stat_date),
            )
            .filter(MetricSnapshot.tenant_id == self.tenant_id)
            .filter(MetricSnapshot.metric_name == metric_name)
            .filter(MetricSnapshot.app_version.isnot(None))
        )
        # 版本分布同样按数据范围统计（否则会泄露无权行的版本构成）
        query = self._apply_row_filters(query, scope, metric_name)
        if start:
            query = query.filter(MetricSnapshot.stat_date >= start)
        if end:
            query = query.filter(MetricSnapshot.stat_date <= end)
        rows = query.group_by(MetricSnapshot.app_version).all()
        return sorted(
            (
                {
                    "app_version": r[0],
                    "rows": r[1],
                    "min_date": r[2].isoformat() if r[2] else None,
                    "max_date": r[3].isoformat() if r[3] else None,
                }
                for r in rows
            ),
            key=lambda item: (-item["rows"], item["app_version"]),
        )

    def list_metrics_covered(self) -> list[str]:
        rows = (
            self.session.query(MetricSnapshot.metric_name)
            .filter(MetricSnapshot.tenant_id == self.tenant_id)
            .distinct()
            .all()
        )
        return sorted(self.visible_metric_names([r[0] for r in rows]))


def pick_baseline_version(versions: list[dict], target: str | None) -> str | None:
    """挑基线版本：优先取数据量最大的"非目标版本"，其次取字典序最大的更早版本。"""
    candidates = [v for v in versions if v.get("app_version") and v.get("app_version") != target]
    if not candidates:
        return None
    if target:
        earlier = [v for v in candidates if _version_key(v["app_version"]) < _version_key(target)]
        if earlier:
            return earlier[0]["app_version"]
    return candidates[0]["app_version"]


def _version_key(version: str) -> tuple:
    parts = []
    for chunk in str(version).replace("-", ".").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)

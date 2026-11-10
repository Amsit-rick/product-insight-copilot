"""标准化只读指标 HTTP 适配器。

上游只允许通过 GET 返回已批准指标的标准化快照。本模块不接受 SQL、任意查询表达式或
写操作地址；每行必须携带租户标识，避免网关配置错误时把其他租户数据导入当前租户。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

import httpx

from app.services.metric_import import CANONICAL_COLUMNS


class MetricSourceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MetricSourceBatch:
    dataset_version: str
    source_version: str
    request_id: str
    records: list[dict]


def validate_metric_source_settings(settings) -> list[str]:
    if not settings.metric_source_http_enabled:
        return []
    endpoint = str(settings.metric_source_http_url or "").strip()
    if not endpoint:
        return ["METRIC_SOURCE_HTTP_ENABLED=true 时必须配置 METRIC_SOURCE_HTTP_URL"]
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return ["METRIC_SOURCE_HTTP_URL 地址格式无效"]
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ["METRIC_SOURCE_HTTP_URL 必须是完整的 HTTP(S) 地址"]
    if settings.app_env == "prod" and parsed.scheme != "https":
        return ["生产环境的 METRIC_SOURCE_HTTP_URL 必须使用 HTTPS"]
    if parsed.username or parsed.password:
        return ["METRIC_SOURCE_HTTP_URL 禁止内嵌凭据"]
    if parsed.query or parsed.fragment:
        return ["METRIC_SOURCE_HTTP_URL 禁止包含查询参数或片段"]
    if not settings.proactive_anomaly_metrics:
        return ["启用指标源前必须配置 PROACTIVE_ANOMALY_METRICS 白名单"]
    return []


class HttpMetricSourceAdapter:
    """从一个固定 GET 端点读取标准化指标批次。"""

    name = "http-metric-source-adapter"
    is_read_only = True

    def __init__(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 5.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.2,
        max_rows: int = 50000,
        max_response_bytes: int = 4 * 1024 * 1024,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.headers = {str(key): str(value) for key, value in (headers or {}).items()}
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_rows = max_rows
        self.max_response_bytes = max_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    @classmethod
    def from_settings(
        cls, settings, *, client: httpx.Client | None = None
    ) -> "HttpMetricSourceAdapter":
        errors = validate_metric_source_settings(settings)
        if errors:
            raise MetricSourceError("metric_source_config_invalid", "；".join(errors))
        return cls(
            endpoint=str(settings.metric_source_http_url).strip(),
            headers=settings.metric_source_http_auth_headers,
            timeout_seconds=settings.metric_source_http_timeout_seconds,
            max_retries=settings.metric_source_http_max_retries,
            retry_backoff_seconds=settings.metric_source_http_retry_backoff_seconds,
            max_rows=settings.metric_source_http_max_rows,
            max_response_bytes=settings.metric_source_http_max_response_bytes,
            client=client,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _backoff(self, attempt: int) -> None:
        delay = self.retry_backoff_seconds * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    def fetch_snapshots(
        self,
        *,
        tenant_id: str,
        metric_names: Sequence[str],
        date_from: date,
        date_to: date,
        request_id: str | None = None,
    ) -> MetricSourceBatch:
        metrics = list(dict.fromkeys(str(item).strip() for item in metric_names if str(item).strip()))
        if not tenant_id or not metrics:
            raise MetricSourceError("metric_source_request_invalid", "租户和指标白名单不能为空")
        if date_from > date_to:
            raise MetricSourceError("metric_source_request_invalid", "指标读取日期范围无效")

        trace_id = request_id or f"metric-sync-{uuid.uuid4().hex}"
        params = {
            "tenant_id": tenant_id,
            "metric_names": ",".join(metrics),
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
        }
        headers = {**self.headers, "X-Request-ID": trace_id}

        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(self.endpoint, headers=headers, params=params)
            except httpx.TimeoutException as exc:
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                raise MetricSourceError("metric_source_timeout", "指标源查询超时") from exc
            except httpx.RequestError as exc:
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                raise MetricSourceError("metric_source_unavailable", "指标源不可用") from exc

            if response.status_code in {408, 429} or response.status_code >= 500:
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                code = "metric_source_rate_limited" if response.status_code == 429 else "metric_source_upstream_error"
                raise MetricSourceError(code, "指标源暂时不可用")
            if response.status_code in {401, 403}:
                raise MetricSourceError("metric_source_auth_error", "指标源鉴权失败")
            if not 200 <= response.status_code < 300:
                raise MetricSourceError("metric_source_request_rejected", "指标源拒绝请求")
            break

        if response is None:  # pragma: no cover - 防御性分支
            raise MetricSourceError("metric_source_unavailable", "指标源不可用")
        if len(response.content) > self.max_response_bytes:
            raise MetricSourceError("metric_source_response_too_large", "指标源响应超过大小限制")
        try:
            body = response.json()
        except ValueError as exc:
            raise MetricSourceError("metric_source_invalid_response", "指标源响应不是有效 JSON") from exc
        if not isinstance(body, Mapping) or not isinstance(body.get("data"), list):
            raise MetricSourceError("metric_source_invalid_response", "指标源响应不符合标准契约")

        raw_records = body["data"]
        if len(raw_records) > self.max_rows:
            raise MetricSourceError("metric_source_too_many_rows", "指标源返回行数超过限制")
        dataset_version = str(body.get("dataset_version") or "").strip()
        if not dataset_version or len(dataset_version) > 64:
            raise MetricSourceError("metric_source_invalid_response", "指标源缺少有效数据版本")
        source_version = str(body.get("source_version") or dataset_version).strip()
        if not source_version or len(source_version) > 64:
            raise MetricSourceError("metric_source_invalid_response", "指标源来源版本无效")
        upstream_request_id = str(body.get("request_id") or trace_id)

        allowed_metrics = set(metrics)
        records: list[dict] = []
        for raw in raw_records:
            if not isinstance(raw, Mapping):
                raise MetricSourceError("metric_source_invalid_response", "指标源数据行格式无效")
            if str(raw.get("tenant_id") or "") != tenant_id:
                raise MetricSourceError(
                    "metric_source_tenant_mismatch", "指标源返回了其他租户的数据"
                )
            metric_name = str(raw.get("metric_name") or "")
            if metric_name not in allowed_metrics:
                raise MetricSourceError(
                    "metric_source_metric_not_allowed", "指标源返回了未授权指标"
                )
            missing = [key for key in ("metric_name", "stat_date", "value") if key not in raw]
            if missing:
                raise MetricSourceError(
                    "metric_source_invalid_response", "指标源数据行缺少必填字段"
                )
            try:
                stat_date = date.fromisoformat(str(raw.get("stat_date") or ""))
            except ValueError as exc:
                raise MetricSourceError(
                    "metric_source_invalid_response", "指标源日期格式无效"
                ) from exc
            if not date_from <= stat_date <= date_to:
                raise MetricSourceError(
                    "metric_source_date_out_of_range", "指标源返回了请求窗口之外的数据"
                )
            value = raw.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise MetricSourceError(
                    "metric_source_invalid_response", "指标源数值字段格式无效"
                )
            record = {key: raw[key] for key in CANONICAL_COLUMNS if key in raw}
            record_source_version = str(record.get("source_version") or source_version).strip()
            if not record_source_version or len(record_source_version) > 64:
                raise MetricSourceError("metric_source_invalid_response", "指标源数据行版本无效")
            record["source_version"] = record_source_version
            records.append(record)

        return MetricSourceBatch(
            dataset_version=dataset_version,
            source_version=source_version,
            request_id=upstream_request_id,
            records=records,
        )


__all__ = [
    "HttpMetricSourceAdapter",
    "MetricSourceBatch",
    "MetricSourceError",
    "validate_metric_source_settings",
]

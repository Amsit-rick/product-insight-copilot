"""标准化订单/支付/物流/售后只读 HTTP 适配器。

上游必须返回 ``{"data": {...}, "observed_at": "ISO-8601", "request_id": "..."}``。
厂商原始字段应在其 API 网关或单独映射层转换为本模块定义的白名单字段；核心业务不猜测
不同 OMS、支付和物流供应商的私有响应结构。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

from app.adapters.base import AdapterResult
from app.models.base import utcnow
from app.services.masking import mask_structure

SOURCES = ("order", "payment", "logistics", "aftersale")
SETTING_FIELDS = {
    "order": "ticket_fact_order_url",
    "payment": "ticket_fact_payment_url",
    "logistics": "ticket_fact_logistics_url",
    "aftersale": "ticket_fact_aftersale_url",
}
ENV_FIELDS = {
    "order": "TICKET_FACT_ORDER_URL",
    "payment": "TICKET_FACT_PAYMENT_URL",
    "logistics": "TICKET_FACT_LOGISTICS_URL",
    "aftersale": "TICKET_FACT_AFTERSALE_URL",
}

# 只允许核心业务已经定义并消费的字段进入事实快照。未知字段（尤其支付流水、账号、凭证）丢弃。
ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "order": frozenset(
        {
            "order_status",
            "order_amount",
            "order_items",
            "order_channel",
            "address",
            "receiver_phone",
            "category",
            "special_category",
        }
    ),
    "payment": frozenset({"paid_amount", "refundable_amount", "pay_channel"}),
    "logistics": frozenset(
        {
            "carrier",
            "tracking_no",
            "latest_track",
            "last_track_at",
            "signed_at",
            "stalled_hours",
            "eta",
            "package_weight",
        }
    ),
    "aftersale": frozenset(
        {
            "aftersale_history",
            "last_case_result",
            "gross_amount",
            "eligible_amount",
            "excluded_items",
            "promotion_threshold",
            "promotion_result",
            "customer_id",
        }
    ),
}


def endpoint_templates(settings) -> dict[str, str]:
    return {source: str(getattr(settings, field, "") or "").strip() for source, field in SETTING_FIELDS.items()}


def validate_endpoint_templates(settings) -> list[str]:
    errors: list[str] = []
    for source, template in endpoint_templates(settings).items():
        env_name = ENV_FIELDS[source]
        if not template:
            errors.append(f"缺少 {env_name}")
        elif "{order_id}" not in template:
            errors.append(f"{env_name} 必须包含 {{order_id}}")
        else:
            try:
                parsed = urlsplit(template.replace("{order_id}", "probe-order"))
            except ValueError:
                errors.append(f"{env_name} 地址格式无效")
                continue
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                errors.append(f"{env_name} 必须使用完整的 http:// 或 https:// 地址")
            elif parsed.username or parsed.password:
                errors.append(f"{env_name} 禁止在 URL 中内嵌用户名或口令")
            elif parsed.fragment:
                errors.append(f"{env_name} 禁止包含 URL 片段")
            elif settings.app_env == "prod" and parsed.scheme != "https":
                errors.append(f"{env_name} 在生产环境必须使用 https://")
    return errors


class HttpFactAdapter:
    """调用四个标准化只读端点，失败时始终返回结构化事实。"""

    name = "http-fact-adapter"
    is_mock = False

    def __init__(
        self,
        *,
        endpoints: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 3.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.2,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoints = {source: str(endpoints.get(source, "")) for source in SOURCES}
        self.headers = {str(key): str(value) for key, value in (headers or {}).items()}
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    @classmethod
    def from_settings(cls, settings) -> "HttpFactAdapter":
        return cls(
            endpoints=endpoint_templates(settings),
            headers=settings.ticket_fact_auth_headers,
            timeout_seconds=settings.ticket_fact_timeout_seconds,
            max_retries=settings.ticket_fact_max_retries,
            retry_backoff_seconds=settings.ticket_fact_retry_backoff_seconds,
        )

    def _failure(self, source: str, request_id: str, code: str, message: str) -> AdapterResult:
        return AdapterResult(
            source=source,
            fact_type="snapshot",
            payload={},
            observed_at=utcnow(),
            request_id=request_id,
            is_mock=False,
            adapter=self.name,
            error={"code": code, "message": message},
        )

    @staticmethod
    def _observed_at(value: object) -> datetime:
        if value in (None, ""):
            return utcnow()
        if not isinstance(value, str):
            raise ValueError("observed_at must be a string")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _safe_payload(source: str, payload: dict) -> dict:
        allowed = ALLOWED_FIELDS[source]
        return {
            key: mask_structure(value, key) for key, value in payload.items() if key in allowed
        }

    def _fetch(
        self,
        source: str,
        order_id: str,
        *,
        customer_id: str | None = None,
        request_id: str | None = None,
    ) -> AdapterResult:
        trace_id = request_id or f"fact-{uuid.uuid4().hex}"
        if not order_id:
            return self._failure(source, trace_id, "missing_order_id", "缺少订单号，无法查询事实")

        template = self.endpoints.get(source, "")
        if not template or "{order_id}" not in template:
            return self._failure(source, trace_id, "adapter_unavailable", f"{source} 接口未配置")

        url = template.replace("{order_id}", quote(order_id, safe=""))
        headers = {**self.headers, "X-Request-ID": trace_id}
        params = {"customer_id": customer_id} if source == "aftersale" and customer_id else None

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(url, headers=headers, params=params)
            except httpx.TimeoutException:
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                return self._failure(source, trace_id, "adapter_timeout", f"{source} 接口超时")
            except httpx.RequestError:
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                return self._failure(source, trace_id, "adapter_unavailable", f"{source} 接口不可用")

            status = response.status_code
            if status == 404:
                # 订单号已经通过请求路径发给上游，不应再复制进错误文本并进入数据库/审计/UI。
                return self._failure(source, trace_id, "order_not_found", "订单不存在")
            if status in {401, 403}:
                return self._failure(source, trace_id, "adapter_auth_error", f"{source} 接口鉴权失败")
            if status == 429 or status >= 500 or status == 408:
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                if status == 429:
                    code, message = "adapter_rate_limited", f"{source} 接口限流"
                elif status == 408:
                    code, message = "adapter_timeout", f"{source} 接口超时"
                else:
                    code, message = "adapter_upstream_error", f"{source} 接口服务异常"
                return self._failure(source, trace_id, code, message)
            if status < 200 or status >= 300:
                return self._failure(source, trace_id, "adapter_request_rejected", f"{source} 接口拒绝请求")

            try:
                body = response.json()
                if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
                    raise ValueError("data must be an object")
                observed_at = self._observed_at(body.get("observed_at"))
            except (ValueError, TypeError):
                return self._failure(
                    source, trace_id, "adapter_invalid_response", f"{source} 接口响应不符合标准契约"
                )

            upstream_request_id = body.get("request_id") or response.headers.get("x-request-id") or trace_id
            return AdapterResult(
                source=source,
                fact_type="snapshot",
                payload=self._safe_payload(source, body["data"]),
                observed_at=observed_at,
                request_id=str(upstream_request_id),
                is_mock=False,
                adapter=self.name,
            )

        raise AssertionError("unreachable")

    def _backoff(self, attempt: int) -> None:
        delay = self.retry_backoff_seconds * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    def close(self) -> None:
        """关闭由适配器自己创建的连接池；注入的测试/共享客户端由调用方管理。"""
        if self._owns_client:
            self._client.close()

    def fetch_order(self, order_id: str, *, request_id: str | None = None) -> AdapterResult:
        return self._fetch("order", order_id, request_id=request_id)

    def fetch_payment(self, order_id: str, *, request_id: str | None = None) -> AdapterResult:
        return self._fetch("payment", order_id, request_id=request_id)

    def fetch_logistics(self, order_id: str, *, request_id: str | None = None) -> AdapterResult:
        return self._fetch("logistics", order_id, request_id=request_id)

    def fetch_aftersale(
        self, order_id: str, *, customer_id: str | None = None, request_id: str | None = None
    ) -> AdapterResult:
        return self._fetch(
            "aftersale", order_id, customer_id=customer_id, request_id=request_id
        )


__all__ = [
    "ALLOWED_FIELDS",
    "ENV_FIELDS",
    "HttpFactAdapter",
    "endpoint_templates",
    "validate_endpoint_templates",
]

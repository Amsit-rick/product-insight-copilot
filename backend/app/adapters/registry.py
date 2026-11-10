"""适配器注册表：按配置选择事实来源。

默认使用模拟适配器（`settings.ticket_use_mock_adapters=True`）；关闭后仅在四个标准化只读 HTTP
端点全部配置正确时启用生产适配器。业务代码（Order Fact Agent、工单服务）不感知切换。

为什么找不到适配器时**不抛异常**：需求书 §4.3 要求"接口过期/超时进人工处理"。
所以这里返回一个"不可用"结果，由调用方落成一条带 error 的事实并让工单进人工，
而不是让整张工单 500。
"""

from __future__ import annotations

import hashlib
import json

from app.adapters.base import AdapterResult, FactAdapter
from app.adapters.http import HttpFactAdapter, endpoint_templates, validate_endpoint_templates
from app.adapters.mcp import describe_mcp_status
from app.adapters.mock import MockFactAdapter
from app.config import Settings, get_settings
from app.models.base import utcnow

_CACHE: dict[str, FactAdapter] = {}


def _build_adapter(settings: Settings) -> FactAdapter:
    if settings.ticket_use_mock_adapters:
        return MockFactAdapter()
    errors = validate_endpoint_templates(settings)
    if errors:
        return _UnavailableAdapter("真实事实接口配置无效：" + "；".join(errors))
    return HttpFactAdapter.from_settings(settings)


class _UnavailableAdapter:
    """未配置真实接口时的占位实现：每次调用返回结构化的"不可用"事实。"""

    name = "unavailable"
    is_mock = False

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def _result(self, source: str, request_id: str | None = None) -> AdapterResult:
        return AdapterResult(
            source=source,
            fact_type="snapshot",
            payload={},
            observed_at=utcnow(),
            request_id=request_id,
            is_mock=False,
            adapter=self.name,
            error={"code": "adapter_unavailable", "message": self._reason},
        )

    def fetch_order(self, order_id: str, *, request_id: str | None = None) -> AdapterResult:
        return self._result("order", request_id)

    def fetch_payment(self, order_id: str, *, request_id: str | None = None) -> AdapterResult:
        return self._result("payment", request_id)

    def fetch_logistics(self, order_id: str, *, request_id: str | None = None) -> AdapterResult:
        return self._result("logistics", request_id)

    def fetch_aftersale(
        self, order_id: str, *, customer_id: str | None = None, request_id: str | None = None
    ) -> AdapterResult:
        return self._result("aftersale", request_id)


def describe_fact_adapter(settings: Settings | None = None) -> dict:
    """返回不含 URL 与鉴权头的配置状态，供健康检查和运维排障。"""
    settings = settings or get_settings()
    if settings.ticket_use_mock_adapters:
        return {"backend": "mock", "configured": True, "is_mock": True, "mcp": describe_mcp_status(settings)}
    endpoints = endpoint_templates(settings)
    errors = validate_endpoint_templates(settings)
    return {
        "backend": "http" if not errors else "unavailable",
        "configured": not errors,
        "is_mock": False,
        "sources": {source: bool(template) for source, template in endpoints.items()},
        "timeout_seconds": settings.ticket_fact_timeout_seconds,
        "max_retries": settings.ticket_fact_max_retries,
        "configuration_errors": errors,
        "mcp": describe_mcp_status(settings),
    }


def _cache_key(settings: Settings) -> str:
    if settings.ticket_use_mock_adapters:
        return "mock"
    material = {
        "endpoints": endpoint_templates(settings),
        "headers": settings.ticket_fact_auth_headers,
        "timeout": settings.ticket_fact_timeout_seconds,
        "retries": settings.ticket_fact_max_retries,
        "backoff": settings.ticket_fact_retry_backoff_seconds,
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    return f"http:{digest}"


def get_fact_adapter(settings: Settings | None = None) -> FactAdapter:
    settings = settings or get_settings()
    key = _cache_key(settings)
    if key not in _CACHE:
        _CACHE[key] = _build_adapter(settings)
    return _CACHE[key]


def reset_adapter_cache() -> None:
    """测试与配置热更新用。"""
    for adapter in _CACHE.values():
        close = getattr(adapter, "close", None)
        if callable(close):
            close()
    _CACHE.clear()

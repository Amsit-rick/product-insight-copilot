"""只读 MCP 事实适配器契约。

传输客户端由部署侧注入，避免在缺少服务地址、认证方式和 MCP SDK/Schema 契约时假装已连通。
MCP 返回值永远只作为不可信业务数据：仅接受结构化对象，并按现有事实字段白名单过滤。
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import urlsplit

from app.adapters.base import AdapterResult
from app.adapters.http import ALLOWED_FIELDS, HttpFactAdapter
from app.models.base import utcnow
from app.services.masking import mask_structure

SOURCES = ("order", "payment", "logistics", "aftersale")
DEFAULT_MCP_TOOL_MAP = {
    "order": "commerce.get_order",
    "payment": "commerce.get_payment",
    "logistics": "commerce.get_logistics",
    "aftersale": "commerce.get_aftersale",
}
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


class McpToolCaller(Protocol):
    """部署侧 MCP 客户端接口；应负责协议握手、传输限时和服务端身份认证。"""

    def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        *,
        context: object | None,
        timeout_seconds: float,
    ) -> object: ...


def validate_mcp_tool_map(tool_map: Mapping[str, str]) -> list[str]:
    """限定四个已批准的只读工具，避免配置误指向任意写操作工具。"""
    if set(tool_map) != set(SOURCES):
        return ["MCP 工具映射必须且只能包含 order/payment/logistics/aftersale"]
    errors = []
    values = list(tool_map.values())
    if len(set(values)) != len(values):
        errors.append("MCP 事实源不能映射到重复工具")
    for source, tool_name in DEFAULT_MCP_TOOL_MAP.items():
        configured = tool_map.get(source)
        if not isinstance(configured, str) or not _TOOL_NAME.fullmatch(configured):
            errors.append(f"{source} MCP 工具名格式无效")
        elif configured != tool_name:
            errors.append(f"{source} 仅允许只读工具 {tool_name}")
    return errors


def validate_mcp_settings(settings) -> list[str]:
    """校验部署侧 MCP 地址；错误信息不包含实际 URL 或凭据。"""
    endpoint = str(settings.ticket_fact_mcp_endpoint or "").strip()
    if not settings.ticket_fact_mcp_enabled and not endpoint:
        return []
    if not endpoint:
        return ["MCP 已启用但未配置服务地址"]
    try:
        parsed = urlsplit(endpoint)
        valid_scheme = parsed.scheme == "https" or (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        if not valid_scheme or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return ["MCP 服务地址无效；仅支持 HTTPS 或本机 HTTP，且不能包含凭据、查询参数或片段"]
    except ValueError:
        return ["MCP 服务地址无效"]
    return []


class McpFactAdapter:
    """调用服务端固定映射的四个只读 MCP 工具，统一转换成 AdapterResult。"""

    name = "mcp-fact-adapter"
    is_mock = False

    def __init__(
        self,
        caller: McpToolCaller | None,
        *,
        tool_map: Mapping[str, str] | None = None,
        timeout_seconds: float = 3.0,
        max_retries: int = 2,
        max_response_bytes: int = 262144,
    ) -> None:
        self.caller = caller
        self.tool_map = dict(tool_map or DEFAULT_MCP_TOOL_MAP)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_response_bytes = max_response_bytes

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

    def _fetch(
        self,
        source: str,
        order_id: str,
        *,
        customer_id: str | None = None,
        request_id: str | None = None,
        context: object | None = None,
    ) -> AdapterResult:
        trace_id = request_id or f"mcp-{uuid.uuid4().hex}"
        errors = validate_mcp_tool_map(self.tool_map)
        if errors:
            return self._failure(source, trace_id, "mcp_tool_mapping_invalid", "MCP 只读工具映射无效")
        if self.caller is None:
            return self._failure(source, trace_id, "mcp_transport_unavailable", "MCP 传输客户端未配置")
        if not order_id:
            return self._failure(source, trace_id, "missing_order_id", "缺少订单号，无法查询事实")

        arguments = {"order_id": order_id}
        if source == "aftersale" and customer_id:
            arguments["customer_id"] = customer_id

        for attempt in range(self.max_retries + 1):
            try:
                response = self.caller.call_tool(
                    self.tool_map[source],
                    arguments,
                    context=context,
                    timeout_seconds=self.timeout_seconds,
                )
            except TimeoutError:
                if attempt < self.max_retries:
                    continue
                return self._failure(source, trace_id, "adapter_timeout", f"{source} MCP 查询超时")
            except Exception:
                if attempt < self.max_retries:
                    continue
                return self._failure(source, trace_id, "adapter_unavailable", f"{source} MCP 查询失败")

            if not isinstance(response, Mapping) or response.get("isError") is True:
                return self._failure(source, trace_id, "adapter_invalid_response", f"{source} MCP 响应不符合标准契约")
            body = response.get("structuredContent", response)
            if not isinstance(body, Mapping) or not isinstance(body.get("data"), Mapping):
                return self._failure(source, trace_id, "adapter_invalid_response", f"{source} MCP 响应不符合标准契约")
            try:
                size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            except (TypeError, ValueError):
                return self._failure(source, trace_id, "adapter_invalid_response", f"{source} MCP 响应不符合标准契约")
            if size > self.max_response_bytes:
                return self._failure(source, trace_id, "adapter_response_too_large", f"{source} MCP 响应超过大小限制")
            try:
                observed_at = HttpFactAdapter._observed_at(body.get("observed_at"))
            except (TypeError, ValueError):
                return self._failure(source, trace_id, "adapter_invalid_response", f"{source} MCP 响应不符合标准契约")

            data = body["data"]
            payload = {
                key: mask_structure(value, key)
                for key, value in data.items()
                if key in ALLOWED_FIELDS[source]
            }
            return AdapterResult(
                source=source,
                fact_type="snapshot",
                payload=payload,
                observed_at=observed_at,
                request_id=str(body.get("request_id") or trace_id),
                is_mock=False,
                adapter=self.name,
            )
        return self._failure(source, trace_id, "adapter_unavailable", f"{source} MCP 查询失败")

    def fetch_order(self, order_id: str, *, request_id: str | None = None, context: object | None = None) -> AdapterResult:
        return self._fetch("order", order_id, request_id=request_id, context=context)

    def fetch_payment(self, order_id: str, *, request_id: str | None = None, context: object | None = None) -> AdapterResult:
        return self._fetch("payment", order_id, request_id=request_id, context=context)

    def fetch_logistics(self, order_id: str, *, request_id: str | None = None, context: object | None = None) -> AdapterResult:
        return self._fetch("logistics", order_id, request_id=request_id, context=context)

    def fetch_aftersale(
        self,
        order_id: str,
        *,
        customer_id: str | None = None,
        request_id: str | None = None,
        context: object | None = None,
    ) -> AdapterResult:
        return self._fetch(
            "aftersale",
            order_id,
            customer_id=customer_id,
            request_id=request_id,
            context=context,
        )


def describe_mcp_status(settings) -> dict:
    """健康状态仅暴露能力标志和静态工具名，不返回地址或任何鉴权信息。"""
    errors = validate_mcp_settings(settings)
    return {
        "enabled": bool(settings.ticket_fact_mcp_enabled),
        "configured": not errors and bool(settings.ticket_fact_mcp_endpoint),
        "transport_available": False,
        "tool_map_valid": not validate_mcp_tool_map(DEFAULT_MCP_TOOL_MAP),
        "tools": dict(DEFAULT_MCP_TOOL_MAP),
        "max_response_bytes": settings.ticket_fact_mcp_max_response_bytes,
        "configuration_errors": errors,
    }


__all__ = [
    "DEFAULT_MCP_TOOL_MAP",
    "McpFactAdapter",
    "McpToolCaller",
    "describe_mcp_status",
    "validate_mcp_settings",
    "validate_mcp_tool_map",
]

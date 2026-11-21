"""Read-only fact providers for the independent integration sandbox."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from sandbox_services.scenarios import (
    load_scenario_manifest,
    public_scenario_catalog,
    validate_scenario_manifest,
)


SOURCES = frozenset({"order", "payment", "logistics", "aftersale"})
SALEOR_VERSION = "3.23.49"
SALEOR_SOURCE_VERSION = f"saleor-{SALEOR_VERSION}"

SALEOR_HEALTH_QUERY = "query SandboxHealth { shop { name } }"
SALEOR_ORDER_QUERY = """query SandboxOrderFacts($numbers: [String!]) {
  orders(first: 2, filter: {numbers: $numbers}) {
    edges {
      node {
        number
        status
        created
        channel { slug }
        total { gross { amount currency } }
        totalCharged { amount currency }
        totalRefunded { amount currency }
        lines { quantity productName variantName }
        transactions {
          authorizedAmount { amount currency }
          chargedAmount { amount currency }
          refundedAmount { amount currency }
          events { type amount { amount currency } }
        }
        fulfillments {
          status
          trackingNumber
          lines {
            quantity
            orderLine { quantity productName variantName }
          }
        }
      }
    }
  }
}"""


ORDER_STATUSES = {
    "DRAFT": "草稿",
    "UNCONFIRMED": "待确认",
    "UNFULFILLED": "待发货",
    "PARTIALLY_FULFILLED": "部分发货",
    "FULFILLED": "已履约",
    "CANCELED": "已取消",
    "EXPIRED": "已过期",
}
FULFILLMENT_STATUSES = {
    "FULFILLED": "已履约",
    "CANCELED": "已取消",
    "REFUNDED": "已退款",
    "REFUNDED_AND_RETURNED": "已退款退货",
    "RETURNED": "已退货",
    "REPLACED": "已换货",
    "WAITING_FOR_APPROVAL": "待审批",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _money(value: Any) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProviderInvalidResponse(
            "adapter_invalid_response", "Saleor amount is invalid"
        ) from exc
    return f"{amount.quantize(Decimal('0.01')):.2f}"


def _amount(node: Any, *path: str) -> Decimal:
    current = node
    for key in path:
        if not isinstance(current, dict) or key not in current:
            raise ProviderInvalidResponse(
                "adapter_invalid_response", "Saleor response is missing amount fields"
            )
        current = current[key]
    try:
        return Decimal(str(current))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProviderInvalidResponse(
            "adapter_invalid_response", "Saleor amount is invalid"
        ) from exc


@dataclass(frozen=True)
class ProviderFact:
    data: dict[str, Any]
    observed_at: str
    source_version: str


class FactProvider(Protocol):
    mode: str

    async def fetch(
        self,
        source: str,
        order_id: str,
        *,
        customer_id: str | None = None,
    ) -> ProviderFact: ...

    async def health(self) -> dict[str, str]: ...

    async def aclose(self) -> None: ...


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.status_code = status_code


class ProviderNotFound(ProviderError):
    def __init__(self) -> None:
        super().__init__("order_not_found", "订单不存在", status_code=404)


class ProviderUnavailable(ProviderError):
    pass


class ProviderInvalidResponse(ProviderError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status_code=502)


class StaticFactProvider:
    mode = "static"
    source_version = "reference-integration-sandbox-v1"

    async def fetch(
        self,
        source: str,
        order_id: str,
        *,
        customer_id: str | None = None,
    ) -> ProviderFact:
        del order_id
        if source == "order":
            data = {
                "order_status": "已签收",
                "order_amount": "268.00",
                "order_items": "SKU-100 x 1",
                "order_channel": "自营商城",
                "address": "上海市浦东新区世纪大道 100 号",
                "receiver_phone": "13812345678",
                "category": "猫粮",
                "unknown_field": "must-be-dropped",
            }
        elif source == "payment":
            data = {
                "paid_amount": "268.00",
                "refundable_amount": "268.00",
                "pay_channel": "支付宝",
                "transaction_id": "must-be-dropped",
            }
        elif source == "logistics":
            data = {
                "carrier": "顺丰速运",
                "tracking_no": "SF1234567890",
                "latest_track": "已签收（本人签收）",
                "signed_at": "2026-09-25T12:00:00+08:00",
                "stalled_hours": 0,
                "eta": "2026-09-25",
                "package_weight": "1.2kg",
            }
        elif source == "aftersale":
            data = {
                "aftersale_history": "2026-09-10 同类商品退货 1 次（已完结）",
                "last_case_result": "退货退款已结案",
                "gross_amount": "268.00",
                "eligible_amount": "268.00",
                "excluded_items": "无",
                "promotion_threshold": "满 199 减 30",
                "promotion_result": "已抵扣",
                "customer_id": customer_id or "SANDBOX-CUST-001",
            }
        else:
            raise ValueError(f"Unsupported fact source: {source}")
        return ProviderFact(
            data=data,
            observed_at=_now_iso(),
            source_version=self.source_version,
        )

    async def health(self) -> dict[str, str]:
        return {"fact_provider": self.mode, "fact_provider_status": "static"}

    async def aclose(self) -> None:
        return None


class SaleorFactProvider:
    mode = "saleor"
    source_version = SALEOR_SOURCE_VERSION

    def __init__(
        self,
        *,
        graphql_url: str,
        app_token: str | None = None,
        app_token_file: str | Path | None = None,
        timeout_seconds: float = 5.0,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.1,
        max_response_bytes: int = 1_000_000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(graphql_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("SALEOR_GRAPHQL_URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("SALEOR_GRAPHQL_URL must not contain credentials or fragments")
        if not app_token and not app_token_file:
            raise ValueError("SALEOR_APP_TOKEN or SALEOR_APP_TOKEN_FILE is required")
        self.graphql_url = graphql_url
        self._app_token = app_token.strip() if app_token else None
        self._app_token_file = Path(app_token_file) if app_token_file else None
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_response_bytes = max_response_bytes
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    def _token(self) -> str:
        if self._app_token:
            return self._app_token
        try:
            token = self._app_token_file.read_text(encoding="utf-8").strip()  # type: ignore[union-attr]
        except OSError as exc:
            raise ProviderUnavailable(
                "adapter_auth_error",
                "Saleor runtime credential is unavailable",
                status_code=503,
            ) from exc
        if not token:
            raise ProviderUnavailable(
                "adapter_auth_error",
                "Saleor runtime credential is unavailable",
                status_code=503,
            )
        return token

    async def _request(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.post(
                    self.graphql_url,
                    headers={"Authorization": f"Bearer {self._token()}"},
                    json={"query": query, "variables": variables},
                    timeout=self.timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
                    continue
                raise ProviderUnavailable(
                    "adapter_timeout", "Saleor query timed out", status_code=504
                ) from exc
            except httpx.RequestError as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
                    continue
                raise ProviderUnavailable(
                    "adapter_unavailable", "Saleor is unavailable", status_code=503
                ) from exc

            if response.status_code in {401, 403}:
                raise ProviderUnavailable(
                    "adapter_auth_error", "Saleor authentication failed", status_code=503
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
                    continue
                code = (
                    "adapter_rate_limited"
                    if response.status_code == 429
                    else "adapter_upstream_error"
                )
                raise ProviderUnavailable(code, "Saleor query failed", status_code=503)
            if not 200 <= response.status_code < 300:
                raise ProviderUnavailable(
                    "adapter_request_rejected",
                    "Saleor rejected the query",
                    status_code=502,
                )
            if len(response.content) > self.max_response_bytes:
                raise ProviderInvalidResponse(
                    "adapter_invalid_response", "Saleor response exceeded the size limit"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderInvalidResponse(
                    "adapter_invalid_response", "Saleor returned invalid JSON"
                ) from exc
            if not isinstance(payload, dict) or payload.get("errors"):
                raise ProviderInvalidResponse(
                    "adapter_invalid_response", "Saleor GraphQL response contained errors"
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ProviderInvalidResponse(
                    "adapter_invalid_response", "Saleor response is missing data"
                )
            return data
        raise AssertionError("unreachable")

    async def _order(self, order_id: str) -> dict[str, Any]:
        data = await self._request(SALEOR_ORDER_QUERY, {"numbers": [order_id]})
        orders = data.get("orders")
        if not isinstance(orders, dict) or not isinstance(orders.get("edges"), list):
            raise ProviderInvalidResponse(
                "adapter_invalid_response", "Saleor response is missing orders"
            )
        edges = orders["edges"]
        if not edges:
            raise ProviderNotFound()
        node = edges[0].get("node") if isinstance(edges[0], dict) else None
        if not isinstance(node, dict) or str(node.get("number")) != str(order_id):
            raise ProviderInvalidResponse(
                "adapter_invalid_response", "Saleor returned an unexpected order"
            )
        return node

    @staticmethod
    def _order_data(node: dict[str, Any]) -> dict[str, Any]:
        lines = node.get("lines")
        if not isinstance(lines, list):
            raise ProviderInvalidResponse(
                "adapter_invalid_response", "Saleor response is missing order lines"
            )
        items = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            name = str(line.get("productName") or line.get("variantName") or "未命名商品")
            quantity = int(line.get("quantity") or 0)
            items.append(f"{name} x {quantity}")
        channel = node.get("channel") or {}
        status = str(node.get("status") or "")
        return {
            "order_status": ORDER_STATUSES.get(status, status or "未知"),
            "order_amount": _money(_amount(node, "total", "gross", "amount")),
            "order_items": ", ".join(items),
            "order_channel": str(channel.get("slug") or "未提供"),
        }

    @staticmethod
    def _payment_data(node: dict[str, Any]) -> dict[str, Any]:
        charged = _amount(node, "totalCharged", "amount")
        refunded = _amount(node, "totalRefunded", "amount")
        return {
            "paid_amount": _money(charged),
            "refundable_amount": _money(max(charged - refunded, Decimal("0"))),
        }

    @staticmethod
    def _logistics_data(node: dict[str, Any]) -> dict[str, Any]:
        fulfillments = node.get("fulfillments")
        if not isinstance(fulfillments, list):
            raise ProviderInvalidResponse(
                "adapter_invalid_response", "Saleor response is missing fulfillments"
            )
        if not fulfillments:
            return {"latest_track": "尚未发货"}
        fulfillment = fulfillments[-1]
        if not isinstance(fulfillment, dict):
            raise ProviderInvalidResponse(
                "adapter_invalid_response", "Saleor fulfillment is invalid"
            )
        status = str(fulfillment.get("status") or "")
        data = {
            "latest_track": FULFILLMENT_STATUSES.get(status, status or "履约状态未知")
        }
        if fulfillment.get("trackingNumber"):
            data["tracking_no"] = str(fulfillment["trackingNumber"])
        return data

    @staticmethod
    def _aftersale_data(
        node: dict[str, Any], customer_id: str | None
    ) -> dict[str, Any]:
        charged = _amount(node, "totalCharged", "amount")
        refunded = _amount(node, "totalRefunded", "amount")
        currency = str((node.get("totalRefunded") or {}).get("currency") or "")
        transactions = node.get("transactions")
        if not isinstance(transactions, list):
            raise ProviderInvalidResponse(
                "adapter_invalid_response", "Saleor response is missing transactions"
            )
        refund_succeeded = any(
            isinstance(event, dict) and event.get("type") == "REFUND_SUCCESS"
            for transaction in transactions
            if isinstance(transaction, dict)
            for event in (transaction.get("events") or [])
        )
        if refunded > 0:
            history = f"已退款 {_money(refunded)} {currency}".strip()
            result = "存在成功退款记录" if refund_succeeded else "存在退款金额记录"
        else:
            history = "未发现退款记录"
            result = "无已知退款"
        data = {
            "aftersale_history": history,
            "last_case_result": result,
            "gross_amount": _money(_amount(node, "total", "gross", "amount")),
            "eligible_amount": _money(max(charged - refunded, Decimal("0"))),
        }
        if customer_id:
            data["customer_id"] = customer_id
        return data

    async def fetch(
        self,
        source: str,
        order_id: str,
        *,
        customer_id: str | None = None,
    ) -> ProviderFact:
        if source not in SOURCES:
            raise ValueError(f"Unsupported fact source: {source}")
        if not order_id:
            raise ValueError("order_id is required")
        node = await self._order(order_id)
        if source == "order":
            data = self._order_data(node)
        elif source == "payment":
            data = self._payment_data(node)
        elif source == "logistics":
            data = self._logistics_data(node)
        else:
            data = self._aftersale_data(node, customer_id)
        return ProviderFact(
            data=data,
            observed_at=_now_iso(),
            source_version=self.source_version,
        )

    async def health(self) -> dict[str, str]:
        try:
            await self._request(SALEOR_HEALTH_QUERY, {})
        except ProviderError:
            return {
                "fact_provider": self.mode,
                "fact_provider_status": "saleor_unavailable",
            }
        return {
            "fact_provider": self.mode,
            "fact_provider_status": "saleor_ready",
        }

    async def aclose(self) -> None:
        await self._client.aclose()


class ScenarioFactProvider:
    """Resolve local scenario aliases, then overlay allowlisted synthetic facts."""

    def __init__(self, base_provider: FactProvider, manifest: dict[str, Any]) -> None:
        self._base_provider = base_provider
        self._manifest = validate_scenario_manifest(manifest)
        self._by_id = {
            str(item["id"]): item for item in self._manifest["scenarios"]
        }
        self.mode = base_provider.mode

    def list_scenarios(self) -> list[dict[str, Any]]:
        return [
            {
                **item,
                "overlays": {
                    source: dict(fields)
                    for source, fields in item["overlays"].items()
                },
            }
            for item in self._manifest["scenarios"]
        ]

    def public_scenarios(self) -> dict[str, Any]:
        return public_scenario_catalog(self._manifest)

    async def fetch(
        self,
        source: str,
        order_id: str,
        *,
        customer_id: str | None = None,
    ) -> ProviderFact:
        scenario = self._by_id.get(order_id)
        if scenario is None:
            return await self._base_provider.fetch(
                source,
                order_id,
                customer_id=customer_id,
            )
        fact = await self._base_provider.fetch(
            source,
            str(scenario["upstream_order_id"]),
            customer_id=customer_id,
        )
        overlay = scenario["overlays"].get(source, {})
        if not overlay:
            return ProviderFact(
                data=dict(fact.data),
                observed_at=fact.observed_at,
                source_version=f"{fact.source_version}+scenario-v1",
            )
        return ProviderFact(
            data={**fact.data, **overlay},
            observed_at=fact.observed_at,
            source_version=f"{fact.source_version}+scenario-v1",
        )

    async def health(self) -> dict[str, str]:
        health = await self._base_provider.health()
        return {**health, "scenario_manifest_status": "ready"}

    async def aclose(self) -> None:
        await self._base_provider.aclose()


def build_fact_provider(environ: Mapping[str, str] | None = None) -> FactProvider:
    values = environ if environ is not None else os.environ
    mode = str(values.get("SANDBOX_FACT_PROVIDER", "static")).strip().lower()
    if mode == "static":
        return StaticFactProvider()
    if mode != "saleor":
        raise ValueError("SANDBOX_FACT_PROVIDER must be static or saleor")
    graphql_url = str(values.get("SALEOR_GRAPHQL_URL", "")).strip()
    if not graphql_url:
        raise ValueError("SALEOR_GRAPHQL_URL is required for saleor provider")
    app_token = str(values.get("SALEOR_APP_TOKEN", "")).strip() or None
    app_token_file = str(values.get("SALEOR_APP_TOKEN_FILE", "")).strip() or None
    if not app_token and not app_token_file:
        raise ValueError(
            "SALEOR_APP_TOKEN or SALEOR_APP_TOKEN_FILE is required for saleor provider"
        )
    provider: FactProvider = SaleorFactProvider(
        graphql_url=graphql_url,
        app_token=app_token,
        app_token_file=app_token_file,
        timeout_seconds=float(values.get("SALEOR_TIMEOUT_SECONDS", "5")),
        max_retries=int(values.get("SALEOR_MAX_RETRIES", "1")),
        retry_backoff_seconds=float(values.get("SALEOR_RETRY_BACKOFF_SECONDS", "0.1")),
        max_response_bytes=int(values.get("SALEOR_MAX_RESPONSE_BYTES", "1000000")),
    )
    manifest_file = str(values.get("SANDBOX_SCENARIO_MANIFEST_FILE", "")).strip()
    if manifest_file:
        provider = ScenarioFactProvider(provider, load_scenario_manifest(manifest_file))
    return provider


__all__ = [
    "FactProvider",
    "ProviderError",
    "ProviderFact",
    "ProviderInvalidResponse",
    "ProviderNotFound",
    "ProviderUnavailable",
    "SALEOR_HEALTH_QUERY",
    "SALEOR_ORDER_QUERY",
    "SaleorFactProvider",
    "ScenarioFactProvider",
    "StaticFactProvider",
    "build_fact_provider",
]

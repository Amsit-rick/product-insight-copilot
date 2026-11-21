"""Deterministic local business scenarios layered over Saleor facts.

The manifest never claims that local overlays are native Saleor facts. It only
stores anonymous scenario aliases, upstream demo order numbers and allowlisted
fact fields needed by the local high-fidelity sandbox.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable


SCENARIO_SCHEMA_VERSION = "saleor-sandbox-scenarios-v1"

REQUIRED_SCENARIO_IDS = (
    "SCN-NORMAL-DELIVERED",
    "SCN-UNPAID-CANCELED",
    "SCN-PAID-UNSHIPPED-REFUND",
    "SCN-PARTIAL-FULFILLMENT",
    "SCN-LOGISTICS-STALLED",
    "SCN-DELIVERED-RETURN",
    "SCN-PARTIAL-REFUND",
    "SCN-DUPLICATE-REFUND",
    "SCN-MISSING-ITEM",
    "SCN-CROSS-SOURCE-CONFLICT",
)

SCENARIO_OVERLAY_FIELDS: dict[str, frozenset[str]] = {
    "order": frozenset(
        {
            "order_status",
            "order_amount",
            "order_items",
            "order_channel",
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
        }
    ),
}

_SENSITIVE_KEY_PARTS = (
    "address",
    "authorization",
    "credential",
    "customer_id",
    "email",
    "password",
    "phone",
    "secret",
    "token",
)
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


class ScenarioManifestError(ValueError):
    """The scenario manifest is unsafe, incomplete or internally inconsistent."""


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ScenarioManifestError(f"order summary {field} must be numeric") from exc


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def _order_sort_key(order: dict[str, Any]) -> tuple[int, int | str]:
    number = str(order["number"])
    return (0, int(number)) if number.isdigit() else (1, number)


def _normalize_orders(order_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_number: dict[str, dict[str, Any]] = {}
    for raw in order_summaries:
        if not isinstance(raw, dict):
            raise ScenarioManifestError("order summaries must be JSON objects")
        number = str(raw.get("number") or "").strip()
        if not number:
            raise ScenarioManifestError("order summary number is required")
        if number in by_number:
            continue
        normalized = {
            "number": number,
            "status": str(raw.get("status") or "").strip().upper(),
            "total_amount": _decimal(raw.get("total_amount", "0"), "total_amount"),
            "charged_amount": _decimal(
                raw.get("charged_amount", "0"), "charged_amount"
            ),
            "refunded_amount": _decimal(
                raw.get("refunded_amount", "0"), "refunded_amount"
            ),
            "has_fulfillment": bool(raw.get("has_fulfillment", False)),
        }
        if min(
            normalized["total_amount"],
            normalized["charged_amount"],
            normalized["refunded_amount"],
        ) < 0:
            raise ScenarioManifestError("order summary amounts must not be negative")
        by_number[number] = normalized
    orders = sorted(by_number.values(), key=_order_sort_key)
    if len(orders) < len(REQUIRED_SCENARIO_IDS):
        raise ScenarioManifestError(
            "at least 10 unique upstream orders are required for deterministic scenarios"
        )
    return orders


def _choose_order(
    orders: list[dict[str, Any]],
    used: set[str],
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    for order in orders:
        if order["number"] not in used and predicate(order):
            used.add(order["number"])
            return order
    for order in orders:
        if order["number"] not in used:
            used.add(order["number"])
            return order
    raise ScenarioManifestError("not enough unique upstream orders")


def _scenario_amount(order: dict[str, Any]) -> Decimal:
    for key in ("charged_amount", "total_amount"):
        if order[key] > 0:
            return order[key]
    return Decimal("100.00")


def _scenario_overlays(scenario_id: str, order: dict[str, Any]) -> dict[str, Any]:
    amount = _scenario_amount(order)
    half = (amount / Decimal("2")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    remainder = amount - half
    common_no_aftersale = {
        "aftersale_history": "未发现售后记录",
        "last_case_result": "无已知售后",
    }

    overlays: dict[str, dict[str, Any]]
    if scenario_id == "SCN-NORMAL-DELIVERED":
        overlays = {
            "order": {"order_status": "已签收"},
            "logistics": {
                "carrier": "本地合成物流",
                "tracking_no": "LOCAL-NORMAL-001",
                "latest_track": "已签收（本地场景）",
                "signed_at": "2026-11-10T10:00:00+08:00",
                "stalled_hours": 0,
            },
            "aftersale": common_no_aftersale,
        }
    elif scenario_id == "SCN-UNPAID-CANCELED":
        overlays = {
            "order": {"order_status": "已取消"},
            "payment": {"paid_amount": "0.00", "refundable_amount": "0.00"},
            "logistics": {"latest_track": "未发货"},
            "aftersale": common_no_aftersale,
        }
    elif scenario_id == "SCN-PAID-UNSHIPPED-REFUND":
        overlays = {
            "order": {"order_status": "待发货"},
            "payment": {
                "paid_amount": _money(amount),
                "refundable_amount": _money(amount),
            },
            "logistics": {"latest_track": "尚未发货"},
            "aftersale": {
                "aftersale_history": "未发现历史退款",
                "last_case_result": "满足退款事实核验条件",
                "gross_amount": _money(amount),
                "eligible_amount": _money(amount),
            },
        }
    elif scenario_id == "SCN-PARTIAL-FULFILLMENT":
        overlays = {
            "order": {"order_status": "部分发货"},
            "logistics": {
                "latest_track": "部分商品已发货（本地场景）",
                "tracking_no": "LOCAL-PARTIAL-001",
            },
        }
    elif scenario_id == "SCN-LOGISTICS-STALLED":
        overlays = {
            "order": {"order_status": "已发货"},
            "logistics": {
                "carrier": "本地合成物流",
                "tracking_no": "LOCAL-STALLED-001",
                "latest_track": "运输中，轨迹长时间未更新",
                "stalled_hours": 72,
            },
        }
    elif scenario_id == "SCN-DELIVERED-RETURN":
        overlays = {
            "order": {"order_status": "已签收"},
            "logistics": {
                "latest_track": "已签收（本地场景）",
                "signed_at": "2026-11-08T09:30:00+08:00",
            },
            "aftersale": {
                "aftersale_history": "已创建退货记录（本地场景）",
                "last_case_result": "退货待验收",
                "gross_amount": _money(amount),
                "eligible_amount": _money(amount),
            },
        }
    elif scenario_id == "SCN-PARTIAL-REFUND":
        overlays = {
            "payment": {
                "paid_amount": _money(amount),
                "refundable_amount": _money(remainder),
            },
            "aftersale": {
                "aftersale_history": f"已部分退款 {_money(half)}（本地场景）",
                "last_case_result": "存在部分退款记录",
                "gross_amount": _money(amount),
                "eligible_amount": _money(remainder),
            },
        }
    elif scenario_id == "SCN-DUPLICATE-REFUND":
        overlays = {
            "payment": {
                "paid_amount": _money(amount),
                "refundable_amount": "0.00",
            },
            "aftersale": {
                "aftersale_history": f"已成功退款 {_money(amount)}（本地场景）",
                "last_case_result": "已存在成功退款记录，需阻止重复退款",
                "gross_amount": _money(amount),
                "eligible_amount": "0.00",
            },
        }
    elif scenario_id == "SCN-MISSING-ITEM":
        overlays = {
            "order": {"order_status": "部分发货"},
            "logistics": {
                "latest_track": "包裹已签收，但履约数量少于订单数量",
                "signed_at": "2026-11-09T15:20:00+08:00",
            },
            "aftersale": {
                "aftersale_history": "已登记少件/漏发（本地场景）",
                "last_case_result": "等待仓库核验",
            },
        }
    elif scenario_id == "SCN-CROSS-SOURCE-CONFLICT":
        overlays = {
            "order": {"order_status": "已支付"},
            "payment": {"paid_amount": "0.00", "refundable_amount": "0.00"},
            "aftersale": {
                "aftersale_history": "未发现退款记录",
                "last_case_result": "订单与支付来源冲突，禁止自动退款",
            },
        }
    else:  # pragma: no cover - guarded by the fixed scenario definitions
        raise ScenarioManifestError(f"unsupported scenario id: {scenario_id}")
    return overlays


_SCENARIO_DEFINITIONS: tuple[
    tuple[str, str, str, Callable[[dict[str, Any]], bool]], ...
] = (
    (
        "SCN-NORMAL-DELIVERED",
        "正常已签收",
        "验证已支付、已签收且无售后的正常事实链",
        lambda item: item["status"] == "FULFILLED"
        and item["charged_amount"] > 0
        and item["refunded_amount"] == 0,
    ),
    (
        "SCN-UNPAID-CANCELED",
        "未支付取消",
        "验证取消订单不可退款",
        lambda item: item["charged_amount"] == 0
        and item["status"] in {"CANCELED", "UNCONFIRMED"},
    ),
    (
        "SCN-PAID-UNSHIPPED-REFUND",
        "已支付未发货退款",
        "验证已支付但未履约时的退款事实核验",
        lambda item: item["status"] == "UNFULFILLED"
        and item["charged_amount"] > 0
        and item["refunded_amount"] == 0,
    ),
    (
        "SCN-PARTIAL-FULFILLMENT",
        "部分发货",
        "验证部分履约不能被误判为全部发货",
        lambda item: item["status"] == "PARTIALLY_FULFILLED",
    ),
    (
        "SCN-LOGISTICS-STALLED",
        "物流停滞",
        "验证轨迹长时间未更新时的异常识别",
        lambda item: item["has_fulfillment"],
    ),
    (
        "SCN-DELIVERED-RETURN",
        "已签收退货",
        "验证签收事实与退货记录的联合判断",
        lambda item: item["status"] == "FULFILLED" or item["has_fulfillment"],
    ),
    (
        "SCN-PARTIAL-REFUND",
        "部分退款",
        "验证已退款金额小于实付金额的剩余可退事实",
        lambda item: Decimal("0")
        < item["refunded_amount"]
        < item["charged_amount"],
    ),
    (
        "SCN-DUPLICATE-REFUND",
        "重复退款请求",
        "验证已有成功退款记录时阻止重复退款",
        lambda item: item["refunded_amount"] > 0,
    ),
    (
        "SCN-MISSING-ITEM",
        "少件或漏发",
        "验证订单数量与履约数量不一致",
        lambda item: item["status"] == "PARTIALLY_FULFILLED",
    ),
    (
        "SCN-CROSS-SOURCE-CONFLICT",
        "跨来源冲突",
        "验证订单与支付来源矛盾时停止自动决策",
        lambda item: item["charged_amount"] > 0,
    ),
)


def build_scenario_manifest(
    order_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the same ten scenario aliases for the same Saleor order set."""

    orders = _normalize_orders(order_summaries)
    used: set[str] = set()
    scenarios: list[dict[str, Any]] = []
    for scenario_id, name, purpose, predicate in _SCENARIO_DEFINITIONS:
        order = _choose_order(orders, used, predicate)
        scenarios.append(
            {
                "id": scenario_id,
                "name": name,
                "purpose": purpose,
                "source_type": "saleor_with_local_overlay",
                "upstream_order_id": order["number"],
                "overlays": _scenario_overlays(scenario_id, order),
            }
        )
    manifest = {
        "schema_version": SCENARIO_SCHEMA_VERSION,
        "environment": "local_high_fidelity_sandbox",
        "enterprise_endpoint_verified": False,
        "real_payment_verified": False,
        "real_carrier_verified": False,
        "production_ready": False,
        "scenarios": scenarios,
    }
    return validate_scenario_manifest(manifest)


def _check_sensitive_content(value: Any, path: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                raise ScenarioManifestError(f"sensitive field is forbidden at {path}.{key}")
            _check_sensitive_content(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_sensitive_content(child, f"{path}[{index}]")
    elif isinstance(value, str) and (_EMAIL_RE.search(value) or _PHONE_RE.search(value)):
        raise ScenarioManifestError(f"sensitive value is forbidden at {path}")


def validate_scenario_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ScenarioManifestError("scenario manifest must be a JSON object")
    _check_sensitive_content(manifest)
    allowed_root = {
        "schema_version",
        "environment",
        "enterprise_endpoint_verified",
        "real_payment_verified",
        "real_carrier_verified",
        "production_ready",
        "scenarios",
    }
    if set(manifest) != allowed_root:
        raise ScenarioManifestError("scenario manifest root fields are invalid")
    if manifest["schema_version"] != SCENARIO_SCHEMA_VERSION:
        raise ScenarioManifestError("unsupported scenario manifest schema version")
    if manifest["environment"] != "local_high_fidelity_sandbox":
        raise ScenarioManifestError("scenario manifest environment is invalid")
    for boundary in (
        "enterprise_endpoint_verified",
        "real_payment_verified",
        "real_carrier_verified",
        "production_ready",
    ):
        if manifest[boundary] is not False:
            raise ScenarioManifestError(f"{boundary} must remain false")
    scenarios = manifest.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != len(REQUIRED_SCENARIO_IDS):
        raise ScenarioManifestError("scenario manifest must contain exactly 10 scenarios")

    allowed_scenario = {
        "id",
        "name",
        "purpose",
        "source_type",
        "upstream_order_id",
        "overlays",
    }
    seen_ids: list[str] = []
    upstream_ids: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != allowed_scenario:
            raise ScenarioManifestError("scenario fields are invalid")
        scenario_id = str(scenario["id"])
        seen_ids.append(scenario_id)
        upstream_order_id = str(scenario["upstream_order_id"] or "").strip()
        if not upstream_order_id or upstream_order_id in upstream_ids:
            raise ScenarioManifestError("scenario upstream orders must be non-empty and unique")
        upstream_ids.add(upstream_order_id)
        if scenario["source_type"] != "saleor_with_local_overlay":
            raise ScenarioManifestError("scenario source type is invalid")
        overlays = scenario["overlays"]
        if not isinstance(overlays, dict) or not overlays:
            raise ScenarioManifestError("scenario overlays must be a non-empty object")
        for source, fields in overlays.items():
            if source not in SCENARIO_OVERLAY_FIELDS:
                raise ScenarioManifestError(f"overlay source {source} is not allowlisted")
            if not isinstance(fields, dict) or not fields:
                raise ScenarioManifestError("overlay fields must be a non-empty object")
            unknown = set(fields) - SCENARIO_OVERLAY_FIELDS[source]
            if unknown:
                raise ScenarioManifestError(
                    f"overlay fields are not allowlisted for {source}: {sorted(unknown)}"
                )
    if tuple(seen_ids) != REQUIRED_SCENARIO_IDS:
        raise ScenarioManifestError("scenario ids or ordering do not match the required set")
    return manifest


def load_scenario_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioManifestError("scenario manifest could not be read") from exc
    return validate_scenario_manifest(payload)


def write_scenario_manifest(
    path: str | Path, order_summaries: list[dict[str, Any]]
) -> dict[str, Any]:
    manifest_path = Path(path)
    if manifest_path.exists():
        return load_scenario_manifest(manifest_path)
    manifest = build_scenario_manifest(order_summaries)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(f"{manifest_path.suffix}.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return manifest


def public_scenario_catalog(manifest: dict[str, Any]) -> dict[str, Any]:
    validated = validate_scenario_manifest(manifest)
    return {
        "environment": validated["environment"],
        "scenarios": [
            {
                "id": scenario["id"],
                "name": scenario["name"],
                "purpose": scenario["purpose"],
                "source_type": scenario["source_type"],
                "overlay_applied": bool(scenario["overlays"]),
            }
            for scenario in validated["scenarios"]
        ],
    }


__all__ = [
    "REQUIRED_SCENARIO_IDS",
    "SCENARIO_OVERLAY_FIELDS",
    "SCENARIO_SCHEMA_VERSION",
    "ScenarioManifestError",
    "build_scenario_manifest",
    "load_scenario_manifest",
    "public_scenario_catalog",
    "validate_scenario_manifest",
    "write_scenario_manifest",
]

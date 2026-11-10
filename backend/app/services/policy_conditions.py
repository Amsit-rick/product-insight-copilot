"""访问策略条件的统一校验与时间判定。

策略条件属于安全边界：管理端允许保存的每个键都必须有明确运行时语义；
历史脏数据即使绕过创建接口，判定时也必须失败关闭，不能静默当作无限制。
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo


class ConditionValidationError(ValueError):
    """策略条件不合法。"""


def _parse_time_of_day(value: Any, *, field: str) -> int:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", text):
        raise ConditionValidationError(f"{field} 必须是 HH:MM")
    hour_text, minute_text = text.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConditionValidationError(f"{field} 超出有效时间范围")
    return hour * 60 + minute


def _timezone(value: Any) -> tzinfo:
    name = str(value or "UTC").strip() or "UTC"
    match = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?", name)
    if match:
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        if hours > 23 or minutes > 59:
            raise ConditionValidationError("timezone 固定偏移无效")
        sign = 1 if match.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise ConditionValidationError(f"timezone 不可用: {name}") from exc


def _policy_timezone(conditions: dict[str, Any]) -> tzinfo:
    window = conditions.get("time_window")
    nested = window.get("timezone") if isinstance(window, dict) else None
    return _timezone(nested or conditions.get("timezone") or "UTC")


def _parse_date(value: Any, *, field: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ConditionValidationError(f"{field} 必须是 ISO 日期或时间") from exc


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ConditionValidationError(f"{field} 必须是数字") from exc


def _validate_string_values(conditions: dict[str, Any], key: str) -> None:
    if key not in conditions:
        return
    raw = conditions.get(key)
    values = raw if isinstance(raw, list) else [raw]
    if not values or any(not str(item or "").strip() for item in values):
        raise ConditionValidationError(f"{key} 必须包含至少一个非空值")


def validate_policy_conditions(
    conditions: dict[str, Any] | None,
    *,
    allow_row_filters: bool,
    row_filter_dimensions: tuple[str, ...] = (),
) -> None:
    """验证管理端允许写入的条件，拒绝未知形态和无效边界。"""

    values = conditions or {}
    if not isinstance(values, dict):
        raise ConditionValidationError("conditions 必须是对象")

    if "min_clearance" in values:
        try:
            clearance = int(values["min_clearance"])
        except (TypeError, ValueError) as exc:
            raise ConditionValidationError("min_clearance 必须是 0 到 9 的整数") from exc
        if not 0 <= clearance <= 9:
            raise ConditionValidationError("min_clearance 必须是 0 到 9 的整数")

    for key in ("departments", "risk_tiers", "classifications", "tool_names"):
        _validate_string_values(values, key)

    minimum = _decimal(values["min_amount"], field="min_amount") if "min_amount" in values else None
    maximum = _decimal(values["max_amount"], field="max_amount") if "max_amount" in values else None
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ConditionValidationError("min_amount 不能大于 max_amount")

    valid_from = _parse_date(values["valid_from"], field="valid_from") if values.get("valid_from") else None
    valid_until = _parse_date(values["valid_until"], field="valid_until") if values.get("valid_until") else None
    if valid_from and valid_until and valid_from.date() > valid_until.date():
        raise ConditionValidationError("valid_from 不能晚于 valid_until")

    if values.get("timezone"):
        _timezone(values["timezone"])

    window = values.get("time_window")
    if window is not None:
        if not isinstance(window, dict) or not window:
            raise ConditionValidationError("time_window 必须是非空对象")
        if not window.get("start") and not window.get("end"):
            raise ConditionValidationError("time_window 至少需要 start 或 end")
        if window.get("start"):
            _parse_time_of_day(window["start"], field="time_window.start")
        if window.get("end"):
            _parse_time_of_day(window["end"], field="time_window.end")
        days = window.get("days")
        if days is not None:
            if not isinstance(days, list) or not days:
                raise ConditionValidationError("time_window.days 必须是 1 到 7 的非空数组")
            try:
                normalized_days = [int(day) for day in days]
            except (TypeError, ValueError) as exc:
                raise ConditionValidationError("time_window.days 只能包含 1 到 7") from exc
            if any(day < 1 or day > 7 for day in normalized_days):
                raise ConditionValidationError("time_window.days 只能包含 1 到 7")
        if window.get("timezone"):
            _timezone(window["timezone"])

    row_filters = values.get("row_filters")
    if row_filters:
        if not allow_row_filters:
            raise ConditionValidationError("row_filters 仅适用于指标类资源")
        if not isinstance(row_filters, dict):
            raise ConditionValidationError("row_filters 必须是对象")
        unknown = sorted(set(row_filters) - set(row_filter_dimensions))
        if unknown:
            raise ConditionValidationError(f"row_filters 包含不支持的维度: {unknown}")
        for dimension, raw in row_filters.items():
            if not isinstance(raw, list) or not raw or any(not str(item or "").strip() for item in raw):
                raise ConditionValidationError(f"row_filters.{dimension} 必须是非空字符串数组")


def evaluate_temporal_conditions(
    conditions: dict[str, Any] | None,
    *,
    occurred_at: datetime | None = None,
) -> tuple[bool, str]:
    """判定有效期与时间窗；历史非法策略一律失败关闭。"""

    values = conditions or {}
    if not any(values.get(key) for key in ("valid_from", "valid_until", "time_window")):
        return True, ""
    try:
        zone = _policy_timezone(values)
        now = occurred_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(zone)

        valid_from = values.get("valid_from")
        if valid_from:
            boundary = _parse_date(valid_from, field="valid_from")
            start = datetime.combine(boundary.date(), time.min, tzinfo=zone)
            if now < start:
                return False, f"该授权 {valid_from} 才生效（尚未生效）"

        valid_until = values.get("valid_until")
        if valid_until:
            boundary = _parse_date(valid_until, field="valid_until")
            end = datetime.combine(boundary.date(), time.max, tzinfo=zone)
            if now > end:
                return False, f"该授权已于 {valid_until} 到期（已过期）"

        window = values.get("time_window")
        if not window:
            return True, ""
        if not isinstance(window, dict):
            raise ConditionValidationError("time_window 必须是对象")
        days = window.get("days")
        if days is not None:
            if not isinstance(days, list) or not days:
                raise ConditionValidationError("time_window.days 必须是非空数组")
            allowed_days = {int(day) for day in days}
            if any(day < 1 or day > 7 for day in allowed_days):
                raise ConditionValidationError("time_window.days 只能包含 1 到 7")
            if now.isoweekday() not in allowed_days:
                return False, "当前日期不在允许的时间窗内"

        start = _parse_time_of_day(window["start"], field="time_window.start") if window.get("start") else None
        end = _parse_time_of_day(window["end"], field="time_window.end") if window.get("end") else None
        if start is None and end is None:
            raise ConditionValidationError("time_window 至少需要 start 或 end")
        current = now.hour * 60 + now.minute
        if start is not None and end is not None:
            allowed = start <= current <= end if start <= end else current >= start or current <= end
        elif start is not None:
            allowed = current >= start
        else:
            allowed = current <= end  # type: ignore[operator]
        if not allowed:
            return False, "当前时间不在允许的时间窗内"
        return True, ""
    except (ConditionValidationError, TypeError, ValueError) as exc:
        return False, f"策略时间条件非法：{exc}"


__all__ = [
    "ConditionValidationError",
    "evaluate_temporal_conditions",
    "validate_policy_conditions",
]

"""反思机制的服务端灰度选择器。

判定是确定性的代码逻辑，不交给模型，也不信任请求侧传入的开关。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping

from app.config import Settings

ReflectionTarget = Literal["ticket_reply", "diagnostic_report"]


@dataclass(frozen=True)
class ReflectionRolloutRequest:
    tenant_id: str
    task_id: str
    target: ReflectionTarget
    risk_level: str | None = None
    issue_type: str | None = None


@dataclass(frozen=True)
class ReflectionRolloutDecision:
    enabled: bool
    reason: str
    bucket: int | None
    percent: int
    matched_scope: dict[str, bool]
    release_status: str
    target: ReflectionTarget

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def stable_reflection_bucket(sample_key: str, salt: str) -> int:
    digest = hashlib.sha256(f"{salt}:{sample_key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100


def _normalised_scope(values: list[str] | tuple[str, ...] | None) -> set[str]:
    return {str(value).strip().lower() for value in (values or []) if str(value).strip()}


def _scope_match(value: str | None, configured: list[str] | None) -> bool:
    scope = _normalised_scope(configured)
    return not scope or str(value or "").strip().lower() in scope


def decide_reflection_rollout(
    request: ReflectionRolloutRequest,
    *,
    settings: Settings,
    release_state: Mapping[str, Any] | None = None,
) -> ReflectionRolloutDecision:
    release_status = str((release_state or {}).get("status") or "active").lower()
    configured_percent = int(
        (release_state or {}).get("current_percent", settings.reflection_rollout_percent)
    )
    percent = max(0, min(100, configured_percent))
    scope = {
        "tenant": _scope_match(request.tenant_id, settings.reflection_rollout_tenants),
        "risk_level": _scope_match(request.risk_level, settings.reflection_rollout_risk_levels),
        "issue_type": _scope_match(request.issue_type, settings.reflection_rollout_issue_types),
    }

    def decision(enabled: bool, reason: str, *, bucket: int | None = None, effective: int | None = None):
        return ReflectionRolloutDecision(
            enabled=enabled,
            reason=reason,
            bucket=bucket,
            percent=percent if effective is None else effective,
            matched_scope=scope,
            release_status=release_status,
            target=request.target,
        )

    if not settings.reflection_enabled:
        return decision(False, "reflection_globally_disabled", effective=0)
    target_enabled = (
        settings.reflection_ticket_enabled
        if request.target == "ticket_reply"
        else settings.reflection_report_enabled
    )
    if not target_enabled:
        return decision(False, "target_disabled", effective=0)
    if release_status == "rolled_back":
        return decision(False, "release_rolled_back", effective=0)
    if release_status == "paused":
        return decision(False, "release_paused", effective=0)
    for key, matched in scope.items():
        if not matched:
            return decision(False, f"{key}_not_in_scope")

    bucket = stable_reflection_bucket(request.task_id, settings.reflection_rollout_salt)
    if percent <= bucket:
        return decision(False, "control_group", bucket=bucket)
    return decision(True, "rollout_enabled", bucket=bucket)


__all__ = [
    "ReflectionRolloutDecision",
    "ReflectionRolloutRequest",
    "decide_reflection_rollout",
    "stable_reflection_bucket",
]

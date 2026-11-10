"""Durable tool contracts: replay safety must be explicit and enforced."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.tools.registry import (
    ReplayPolicy,
    SideEffectLevel,
    ToolContext,
    ToolRegistry,
    ToolSpec,
)


class _AllowPolicy:
    def check_tool_call(self, agent_code: str, name: str, arguments: dict):  # noqa: ANN001
        return SimpleNamespace(allowed=True, reason="allowed")


def _registry(monkeypatch) -> ToolRegistry:  # noqa: ANN001
    registry = ToolRegistry(policy=_AllowPolicy())
    monkeypatch.setattr(registry, "_persist", lambda *args, **kwargs: None)
    return registry


def _context(*, replaying: bool = False, approved: bool = False) -> ToolContext:
    return ToolContext(
        session=object(),
        task_id="task-durable-tool",
        tenant_id="tenant-durable-tool",
        user_id="user-durable-tool",
        agent_code="order_agent",
        replaying=replaying,
        approved_tool_calls={"refund.execute"} if approved else set(),
        approval_reference="approval-001" if approved else None,
    )


def test_describe_exposes_explicit_replay_and_side_effect_contract(monkeypatch) -> None:
    registry = _registry(monkeypatch)
    registry.register(
        ToolSpec(
            name="order.read",
            description="read order",
            handler=lambda **_: {"ok": True},
            replay_policy=ReplayPolicy.SAFE,
            side_effect_level=SideEffectLevel.READ_ONLY,
            requires_session=False,
        )
    )

    assert registry.describe() == [
        {
            "name": "order.read",
            "description": "read order",
            "arg_schema": {},
            "replay_policy": "safe",
            "side_effect_level": "read_only",
            "idempotency_scope": None,
            "requires_approval": False,
        }
    ]


def test_safe_replay_is_reserved_for_read_only_tools(monkeypatch) -> None:
    registry = _registry(monkeypatch)
    with pytest.raises(ValueError, match="safe"):
        registry.register(
            ToolSpec(
                name="message.send.unsafe-contract",
                description="invalid contract",
                handler=lambda **_: {},
                replay_policy=ReplayPolicy.SAFE,
                side_effect_level=SideEffectLevel.REVERSIBLE,
                requires_session=False,
            )
        )


def test_idempotent_tool_requires_stable_key_before_execution(monkeypatch) -> None:
    registry = _registry(monkeypatch)
    calls: list[dict] = []
    registry.register(
        ToolSpec(
            name="message.send",
            description="send a customer message once",
            handler=lambda **kwargs: calls.append(kwargs) or {"sent": True},
            replay_policy=ReplayPolicy.IDEMPOTENT,
            side_effect_level=SideEffectLevel.REVERSIBLE,
            idempotency_scope="ticket",
            requires_session=False,
        )
    )

    denied = registry.call("message.send", {"content": "hello"}, _context())
    assert denied.ok is False
    assert denied.policy_decision == "deny"
    assert "幂等键" in (denied.error or "")
    assert calls == []

    allowed = registry.call(
        "message.send",
        {"content": "hello", "idempotency_key": "ticket-001:message-001"},
        _context(),
    )
    assert allowed.ok is True
    assert allowed.idempotency_key == "ticket-001:message-001"
    assert len(calls) == 1


def test_unsafe_replay_requires_persisted_human_approval(monkeypatch) -> None:
    registry = _registry(monkeypatch)
    executions: list[str] = []
    registry.register(
        ToolSpec(
            name="refund.execute",
            description="execute refund",
            handler=lambda **_: executions.append("refund") or {"submitted": True},
            replay_policy=ReplayPolicy.UNSAFE,
            side_effect_level=SideEffectLevel.IRREVERSIBLE,
            requires_approval=True,
            requires_session=False,
        )
    )

    denied = registry.call("refund.execute", {}, _context(replaying=True))
    assert denied.ok is False
    assert denied.policy_decision == "require_approval"
    assert executions == []

    still_blocked = registry.call(
        "refund.execute",
        {},
        _context(replaying=True, approved=True),
    )
    assert still_blocked.ok is False
    assert still_blocked.policy_decision == "require_approval"
    assert executions == []

    approved = registry.call(
        "refund.execute",
        {},
        _context(replaying=False, approved=True),
    )
    assert approved.ok is True
    assert approved.replayed is False
    assert approved.approval_reference == "approval-001"
    assert executions == ["refund"]


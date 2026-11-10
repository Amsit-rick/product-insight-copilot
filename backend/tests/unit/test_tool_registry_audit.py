"""工具调用审计必须保留结构，并安全写入 JSON 字段。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.db import session as db_session
from app.db.session import build_session_factory
from app.models.workflow import ToolCall
from app.tools.registry import ToolContext, ToolOutcome, ToolRegistry


def test_tool_audit_normalizes_non_json_values_and_persists(monkeypatch, engine) -> None:
    factory = build_session_factory(engine)
    real_write_scope = db_session.write_scope
    monkeypatch.setattr(db_session, "write_scope", lambda: real_write_scope(factory))

    context_session = factory()
    try:
        ctx = ToolContext(
            session=context_session,
            task_id="task-json-audit",
            tenant_id="tenant-json-audit",
            user_id="user-json-audit",
            agent_code="order_agent",
        )
        ToolRegistry()._persist(
            ctx,
            "order.facts",
            {
                "requested_at": datetime(2026, 11, 6, 10, 30, 45),
                "api_token": "secret-token-value",
            },
            ToolOutcome(
                ok=True,
                tool_name="order.facts",
                data={
                    "observed_at": datetime(2026, 11, 6, 10, 31, 2),
                    "settlement_date": date(2026, 11, 5),
                    "amount": Decimal("123.4500"),
                    "nested": [{"fee": Decimal("0.0100")}],
                },
            ),
        )
    finally:
        context_session.close()

    verification = factory()
    try:
        row = verification.query(ToolCall).filter_by(task_id="task-json-audit").one()
        assert row.arguments["requested_at"] == "2026-11-06T10:30:45"
        assert row.arguments["api_token"] != "secret-token-value"
        assert row.result == {
            "observed_at": "2026-11-06T10:31:02",
            "settlement_date": "2026-11-05",
            "amount": "123.4500",
            "nested": [{"fee": "0.0100"}],
        }
        assert len(row.result_digest or "") == 64
    finally:
        verification.close()

"""SSE 事件流与每单成本的单测。

两件事都必须钉住，因为它们在**故障时才被依赖**：

1. SSE 帧格式：`data` 里出现换行会破坏帧边界（前端解析直接断），必须单行 JSON；
2. 成本累计口径：一张工单可能被分析多次，按"最近一次"算会低估成本——
   这正是"评审时觉得便宜、上线到高频却失控"的常见来源。
"""

from __future__ import annotations

import json

from app.services.ticket_observability import (
    _sse,
    estimate_batch_cost,
    event_stream,
    ticket_cost,
)


class _FakeEvent:
    def __init__(self, node: str, message: str, payload: dict | None = None) -> None:
        self.id = f"e-{node}-{message[:4]}"
        self.node = node
        self.level = "info"
        self.message = message
        self.payload = payload or {}
        self.created_at = None


class _FakeTask:
    def __init__(self, status: str) -> None:
        self.status = status


class _FakeQuery:
    def __init__(self, rows: list[_FakeEvent]) -> None:
        self._rows = rows

    def filter(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self

    def order_by(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, rows: list[_FakeEvent], status: str) -> None:
        self._rows = rows
        self._task = _FakeTask(status)
        self.closed = False

    def query(self, model):  # noqa: ANN001
        return _FakeQuery(self._rows)

    def get(self, model, key):  # noqa: ANN001
        return self._task

    def close(self) -> None:
        self.closed = True


def test_sse_frame_is_single_line_json():
    frame = _sse("triage_agent", {"message": "第一行\n第二行", "node": "triage_agent"})
    lines = frame.strip().split("\n")
    assert lines[0] == "event: triage_agent"
    assert lines[1].startswith("data: ")
    # data 必须是单行：换行会被当作帧结束，前端只能解析到半截 JSON
    assert len(lines) == 2
    payload = json.loads(lines[1][len("data: ") :])
    assert payload["message"] == "第一行\n第二行"
    assert frame.endswith("\n\n")


def test_event_stream_emits_events_then_done_on_terminal_status():
    rows = [_FakeEvent("triage_agent", "分类完成"), _FakeEvent("resolution_agent", "方案完成")]
    sessions: list[_FakeSession] = []

    def factory() -> _FakeSession:
        session = _FakeSession(rows, status="completed")
        sessions.append(session)
        return session

    frames = list(event_stream(factory, "run-1", poll_seconds=0.0, max_seconds=5))
    assert frames[0].startswith("event: open")
    assert any(frame.startswith("event: triage_agent") for frame in frames)
    assert any(frame.startswith("event: resolution_agent") for frame in frames)
    # 终态必须发 done 并收尾，否则前端一直挂着连接
    assert frames[-1].startswith("event: done")
    assert json.loads(frames[-1].strip().split("\n")[1][len("data: ") :])["status"] == "completed"
    assert all(session.closed for session in sessions), "每个轮询都必须是新会话并及时关闭"


def test_event_stream_times_out_gracefully():
    def factory() -> _FakeSession:
        return _FakeSession([], status="running")

    frames = list(event_stream(factory, "run-2", poll_seconds=0.0, max_seconds=0.01))
    assert frames[-1].startswith("event: timeout"), frames[-1]


def test_estimate_batch_cost_gives_arithmetic():
    """把"能不能用在高频"变成算术：单均成本 × 日单量 → 日/月/年。"""
    result = estimate_batch_cost(0.13, 10000)
    assert result["daily_amount"] == 1300.0
    assert result["monthly_amount"] == 39000.0
    assert result["yearly_amount"] == 474500.0
    # 低频场景：每天 30 单
    small = estimate_batch_cost(0.13, 30)
    assert small["daily_amount"] < 5


def test_estimate_batch_cost_handles_zero_volume():
    result = estimate_batch_cost(0.13, 0)
    assert result["daily_amount"] == 0.0 and result["per_ticket_amount"] == 0.13


def test_ticket_cost_aggregates_all_analyses(monkeypatch):
    """成本按**累计**口径：一张工单被分析两次就是两倍，不能只看最近一次。"""

    class _Ticket:
        id = "t1"
        last_analysis_task_id = "task-2"

    monkeypatch.setattr(
        "app.services.ticket_observability.ticket_analysis_task_ids",
        lambda session, ticket_id: ["task-1", "task-2"],
    )

    class _Breakdown:
        currency = "CNY"
        input_tokens = 1000
        output_tokens = 500
        cache_hit_tokens = 100
        amount = 0.05
        calls = 2
        per_model = {"deepseek-chat": {"input_tokens": 1000, "output_tokens": 500, "amount": 0.05, "calls": 2}}
        unpriced_models: list[str] = []

        def __init__(self) -> None:
            pass

        @property
        def total_tokens(self) -> int:
            return 1500

        def as_dict(self) -> dict:
            return {
                "currency": "CNY",
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_hit_tokens": self.cache_hit_tokens,
                "amount": self.amount,
                "calls": self.calls,
                "total_tokens": self.total_tokens,
                "per_model": self.per_model,
                "unpriced_models": self.unpriced_models,
            }

    monkeypatch.setattr(
        "app.services.ticket_observability.task_cost", lambda session, task_id: _Breakdown()
    )

    payload = ticket_cost(object(), _Ticket())
    assert payload["analysis_count"] == 2
    assert payload["amount"] == round(0.1, 6)
    assert payload["total_tokens"] == 3000
    assert payload["calls"] == 4
    assert len(payload["per_analysis"]) == 2
    assert payload["amount_per_analysis"] == round(0.05, 6)
    assert payload["per_model"]["deepseek-chat"]["amount"] == 0.1


def test_ticket_cost_without_analysis_is_zero(monkeypatch):
    class _Ticket:
        id = "t0"
        last_analysis_task_id = None

    # 无分析的工单不该去查任务表（也避免用假 session 去打真查询）
    monkeypatch.setattr(
        "app.services.ticket_observability.ticket_analysis_task_ids",
        lambda session, ticket_id: [],
    )
    payload = ticket_cost(object(), _Ticket())
    assert payload["analysis_count"] == 0
    assert payload["amount"] == 0.0
    assert payload["amount_per_analysis"] == 0.0

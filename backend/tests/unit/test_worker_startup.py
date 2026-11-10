"""独立 worker 冷启动时的 schema 就绪门禁。"""

from __future__ import annotations

from app import worker as worker_module


def test_wait_for_queue_schema_retries_until_api_has_initialized_database(monkeypatch):
    states = iter([False, False, True])
    sleeps: list[float] = []

    monkeypatch.setattr(worker_module, "queue_schema_ready", lambda: next(states))
    monkeypatch.setattr(worker_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    worker_module.wait_for_queue_schema(timeout_seconds=10, poll_interval_seconds=0.25)

    assert sleeps == [0.25, 0.25]


def test_wait_for_queue_schema_times_out_instead_of_looping_forever(monkeypatch):
    clock = iter([0.0, 0.1, 0.6])
    monkeypatch.setattr(worker_module, "queue_schema_ready", lambda: False)
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(worker_module.time, "sleep", lambda _seconds: None)

    try:
        worker_module.wait_for_queue_schema(timeout_seconds=0.5, poll_interval_seconds=0.1)
    except RuntimeError as exc:
        assert "task_queue_items" in str(exc)
    else:  # pragma: no cover - 断言辅助
        raise AssertionError("schema 长期未就绪时必须超时失败")

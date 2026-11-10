"""生产形态工单负载验证脚本的回归测试。

这组测试不启动 Docker；它固定住负载验证的统计口径、运行时硬门槛，以及
Compose/依赖必须满足的部署契约。真实容器验收由脚本在隔离 Compose project 中完成。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
SCRIPT_PATH = BACKEND / "scripts" / "benchmark_ticket_queue.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("benchmark_ticket_queue_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load_script()


def _healthy_payload() -> dict:
    return {
        "database": "postgres",
        "llm": {"provider": "stub", "enabled": False},
        "embedding": {"provider": "stub", "enabled": False},
        "cache": {
            "backend": "redis",
            "configured": "redis",
            "healthy": True,
            "degraded_reason": None,
        },
        "execution": {
            "backend": "queue",
            "configured_backend": "queue",
            "checkpointer": {"backend": "postgres", "shared": True},
            "queue": {"pending": 0, "claimed": 0, "done": 20, "dead": 0},
        },
    }


def test_production_compose_defaults_to_postgres_checkpointer():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    requirements = (BACKEND / "requirements-runtime.in").read_text(encoding="utf-8")

    assert compose.count("CHECKPOINTER_BACKEND: ${CHECKPOINTER_BACKEND:-postgres}") >= 2
    assert "CHECKPOINTER_POSTGRES_URL:" in compose
    worker_section = compose.split("\n  worker:\n", 1)[1]
    assert "healthcheck:\n      disable: true" in worker_section
    assert "langgraph-checkpoint-postgres" in requirements


def test_ticket_load_overlay_keeps_migration_gate():
    overlay = (ROOT / "deploy" / "docker-compose.ticket-load.yml").read_text(encoding="utf-8")

    assert overlay.count("migrate:\n        condition: service_completed_successfully") == 2
    assert overlay.count('TASK_SUBMIT_RATE_LIMIT_PER_MINUTE: "5"') == 2
    assert overlay.count('WORKER_LEASE_SECONDS: "20"') == 2


def test_load_script_uses_valid_api_channel_and_marks_test_data():
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert '"channel": "api"' in source
    assert '"data_origin": "test"' in source
    assert '"channel": "loadtest"' not in source


def test_load_script_keeps_documented_submit_p95_baseline():
    args = bench.parse_args([])

    assert args.target_submit_p95 == 3_000


def test_health_gate_accepts_only_real_shared_production_shape():
    assert bench.validate_runtime_health(_healthy_payload()) == []


def test_health_gate_rejects_degraded_or_local_backends():
    cases = []

    sqlite_db = _healthy_payload()
    sqlite_db["database"] = "sqlite"
    cases.append(sqlite_db)

    fallback_cache = _healthy_payload()
    fallback_cache["cache"] = {
        "backend": "memory",
        "configured": "redis",
        "healthy": False,
        "degraded_reason": "Redis unavailable",
    }
    cases.append(fallback_cache)

    inline_execution = _healthy_payload()
    inline_execution["execution"]["backend"] = "threadpool"
    cases.append(inline_execution)

    local_checkpoint = _healthy_payload()
    local_checkpoint["execution"]["checkpointer"] = {
        "backend": "sqlite",
        "shared": True,
    }
    cases.append(local_checkpoint)

    for payload in cases:
        assert bench.validate_runtime_health(payload), payload


def test_health_gate_refuses_real_model_costs_by_default():
    payload = _healthy_payload()
    payload["llm"] = {"provider": "openai_compatible", "enabled": True}
    assert any("stub" in error.lower() for error in bench.validate_runtime_health(payload))


def test_percentile_and_report_metrics_include_both_latency_stages():
    submissions = [
        {"task_id": "task-1", "latency_ms": 100.0, "ok": True, "started_at_mono": 10.0},
        {"task_id": "task-2", "latency_ms": 200.0, "ok": True, "started_at_mono": 11.0},
        {"task_id": "task-3", "latency_ms": 300.0, "ok": False, "error": "HTTP 500", "started_at_mono": 12.0},
    ]
    completions = [
        {"task_id": "task-1", "latency_ms": 800.0, "ok": True},
        {"task_id": "task-2", "latency_ms": 1_200.0, "ok": True},
        {"task_id": "task-3", "latency_ms": 2_000.0, "ok": False, "error": "failed"},
    ]
    queue_items = [
        {"task_id": "task-1", "claimed_by": "worker-a"},
        {"task_id": "task-2", "claimed_by": "worker-b"},
    ]

    report = bench.summarize_run(
        submissions,
        completions,
        queue_items=queue_items,
        elapsed_seconds=2.0,
        final_health=_healthy_payload(),
    )

    assert bench.percentile([100.0, 200.0, 300.0], 0.95) == 300.0
    assert report["latency_ms"]["submit_p95"] == 300.0
    assert report["latency_ms"]["end_to_end_p95"] == 2_000.0
    assert report["counts"] == {"requested": 3, "submitted": 2, "completed": 2, "errors": 1}
    assert report["error_rate"] == 1 / 3
    assert report["throughput_per_second"] == 1.0
    assert report["workers"] == ["worker-a", "worker-b"]
    assert report["queue_final"] == {"pending": 0, "claimed": 0, "done": 20, "dead": 0}


def test_acceptance_fails_for_worker_shortage_or_nonempty_queue():
    report = {
        "latency_ms": {"submit_p95": 100.0, "end_to_end_p95": 1_000.0},
        "error_rate": 0.0,
        "workers": ["worker-a"],
        "queue_final": {"pending": 1, "claimed": 0, "done": 1, "dead": 0},
    }
    checks = bench.evaluate_acceptance(
        report,
        target_submit_p95_ms=1_000.0,
        target_end_to_end_p95_ms=20_000.0,
        max_error_rate=0.0,
        expected_workers=2,
    )

    failed = {item["name"] for item in checks if not item["passed"]}
    assert "独立 worker 数" in failed
    assert "队列最终清空" in failed
    assert bench.exit_code(checks) == 1


def test_acceptance_returns_zero_only_when_all_hard_gates_pass():
    report = {
        "latency_ms": {"submit_p95": 200.0, "end_to_end_p95": 2_000.0},
        "error_rate": 0.0,
        "workers": ["worker-a", "worker-b"],
        "queue_final": {"pending": 0, "claimed": 0, "done": 10, "dead": 0},
    }
    checks = bench.evaluate_acceptance(
        report,
        target_submit_p95_ms=1_000.0,
        target_end_to_end_p95_ms=20_000.0,
        max_error_rate=0.0,
        expected_workers=2,
    )
    assert all(item["passed"] for item in checks)
    assert bench.exit_code(checks) == 0

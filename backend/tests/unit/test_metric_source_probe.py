from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from app.adapters.metric_http import MetricSourceBatch, MetricSourceError
from app.config import Settings


BACKEND = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "verify_metric_source", BACKEND / "scripts" / "verify_metric_source.py"
)
assert _SPEC and _SPEC.loader
probe = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = probe
_SPEC.loader.exec_module(probe)


def _settings(**overrides) -> Settings:
    values = {
        "llm_provider": "stub",
        "metric_source_http_enabled": True,
        "metric_source_http_url": "https://metrics.example.test/snapshots",
        "metric_source_http_auth_headers": {"Authorization": "Bearer hidden"},
        "proactive_anomaly_metrics": ["next_day_retention"],
        "proactive_anomaly_lookback_days": 30,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_probe_defaults_to_plan_only_without_network(tmp_path):
    output = tmp_path / "metric-source.json"

    def forbidden(_settings):
        raise AssertionError("未显式授权时禁止创建网络适配器")

    exit_code = probe.main(
        ["--tenant-id", "tenant-a", "--json-output", str(output)],
        settings=_settings(),
        adapter_factory=forbidden,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert payload["status"] == "blocked_not_authorized"
    assert payload["external_network_used"] is False
    assert payload["production_ready"] is False
    assert payload["generated_at"]
    assert payload["generated_at"].endswith("+00:00")
    assert payload["configuration"]["runs_requested"] == 3


def test_authorized_probe_reports_only_sanitized_metadata(tmp_path):
    output = tmp_path / "metric-source.json"

    class StubAdapter:
        def fetch_snapshots(self, **_kwargs):
            return MetricSourceBatch(
                dataset_version="ds-1",
                source_version="warehouse-1",
                request_id="secret-upstream-request-id",
                records=[
                    {
                        "metric_name": "next_day_retention",
                        "stat_date": "2026-11-07",
                        "value": 0.42,
                    }
                ],
            )

        def close(self):
            pass

    exit_code = probe.main(
        [
            "--tenant-id",
            "tenant-a",
            "--allow-real-source-call",
            "--json-output",
            str(output),
        ],
        settings=_settings(),
        adapter_factory=lambda _settings: StubAdapter(),
    )

    raw = output.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert exit_code == 0
    assert payload["status"] == "verified_read_only"
    assert payload["external_network_used"] is True
    assert payload["row_count"] == 1
    assert payload["metric_names"] == ["next_day_retention"]
    assert payload["date_range"] == {"min": "2026-11-07", "max": "2026-11-07"}
    assert payload["summary"]["runs_completed"] == 3
    assert payload["summary"]["metric_coverage_complete"] is True
    assert payload["summary"]["latency_within_limit"] is True
    assert len(payload["runs"]) == 3
    assert payload["configuration"] == {
        "app_env": "local",
        "enabled": True,
        "read_method": "GET",
        "https": True,
        "auth_configured": True,
        "tenant_scope_provided": True,
        "metric_whitelist_count": 1,
        "runs_requested": 3,
    }
    assert payload["production_ready"] is False
    assert "secret-upstream-request-id" not in raw
    assert "Bearer hidden" not in raw
    assert "metrics.example.test" not in raw
    assert "0.42" not in raw


def test_authorized_probe_requires_every_run_to_cover_metric_whitelist(tmp_path):
    output = tmp_path / "metric-source.json"

    class PartialAdapter:
        def fetch_snapshots(self, **_kwargs):
            return MetricSourceBatch(
                dataset_version="ds-1",
                source_version="warehouse-1",
                request_id="hidden",
                records=[
                    {
                        "metric_name": "next_day_retention",
                        "stat_date": "2026-11-07",
                        "value": 0.42,
                    }
                ],
            )

        def close(self):
            pass

    exit_code = probe.main(
        [
            "--tenant-id",
            "tenant-a",
            "--allow-real-source-call",
            "--json-output",
            str(output),
        ],
        settings=_settings(proactive_anomaly_metrics=["next_day_retention", "crash_rate"]),
        adapter_factory=lambda _settings: PartialAdapter(),
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["error_code"] == "metric_source_acceptance_failed"
    assert payload["summary"]["metric_coverage_complete"] is False


def test_authorized_probe_sanitizes_failure(tmp_path):
    output = tmp_path / "metric-source.json"

    class FailingAdapter:
        def fetch_snapshots(self, **_kwargs):
            raise MetricSourceError("metric_source_auth_error", "secret body")

        def close(self):
            pass

    exit_code = probe.main(
        [
            "--tenant-id",
            "tenant-a",
            "--allow-real-source-call",
            "--json-output",
            str(output),
        ],
        settings=_settings(),
        adapter_factory=lambda _settings: FailingAdapter(),
    )

    raw = output.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["error_code"] == "metric_source_auth_error"
    assert payload["external_network_used"] is True
    assert "secret body" not in raw

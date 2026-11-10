from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.adapters.metric_http import (
    HttpMetricSourceAdapter,
    MetricSourceError,
    validate_metric_source_settings,
)
from app.config import Settings
from app.models.metrics import MetricSnapshot
from app.services.metric_source_sync import sync_metric_source


def _settings(**overrides) -> Settings:
    values = {
        "llm_provider": "stub",
        "metric_source_http_enabled": True,
        "metric_source_http_url": "https://metrics.example.test/api/v1/snapshots",
        "metric_source_http_auth_headers": {"Authorization": "Bearer server-only"},
        "metric_source_http_timeout_seconds": 3,
        "metric_source_http_max_retries": 0,
        "metric_source_http_max_rows": 100,
        "proactive_anomaly_metrics": ["next_day_retention", "crash_rate"],
        "proactive_anomaly_lookback_days": 30,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_disabled_metric_source_needs_no_endpoint():
    settings = _settings(metric_source_http_enabled=False, metric_source_http_url="")

    assert validate_metric_source_settings(settings) == []


def test_enabled_metric_source_requires_safe_endpoint():
    missing = validate_metric_source_settings(
        _settings(metric_source_http_url="")
    )
    insecure_prod = validate_metric_source_settings(
        _settings(app_env="prod", metric_source_http_url="http://metrics.example.test/snapshots")
    )
    embedded_secret = validate_metric_source_settings(
        _settings(metric_source_http_url="https://user:secret@metrics.example.test/snapshots")
    )

    assert "METRIC_SOURCE_HTTP_URL" in missing[0]
    assert "HTTPS" in insecure_prod[0]
    assert "凭据" in embedded_secret[0]


def test_http_metric_source_reads_only_requested_tenant_and_metrics():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["authorization"] = request.headers.get("Authorization")
        seen["request_id"] = request.headers.get("X-Request-ID")
        seen["query"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "dataset_version": "metrics-20261108-01",
                "source_version": "warehouse-job-88",
                "request_id": "upstream-req-1",
                "data": [
                    {
                        "tenant_id": "tenant-a",
                        "metric_name": "next_day_retention",
                        "platform": "android",
                        "stat_date": "2026-11-07",
                        "value": 0.42,
                        "sample_size": 1200,
                        "unapproved_internal_note": "must-not-enter-database",
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HttpMetricSourceAdapter.from_settings(_settings(), client=client)

    batch = adapter.fetch_snapshots(
        tenant_id="tenant-a",
        metric_names=["next_day_retention", "crash_rate"],
        date_from=date(2026, 10, 9),
        date_to=date(2026, 11, 8),
        request_id="metric-sync-1",
    )

    assert seen == {
        "method": "GET",
        "authorization": "Bearer server-only",
        "request_id": "metric-sync-1",
        "query": {
            "tenant_id": "tenant-a",
            "metric_names": "next_day_retention,crash_rate",
            "date_from": "2026-10-09",
            "date_to": "2026-11-08",
        },
    }
    assert batch.dataset_version == "metrics-20261108-01"
    assert batch.source_version == "warehouse-job-88"
    assert batch.request_id == "upstream-req-1"
    assert batch.records == [
        {
            "metric_name": "next_day_retention",
            "platform": "android",
            "stat_date": "2026-11-07",
            "value": 0.42,
            "sample_size": 1200,
            "source_version": "warehouse-job-88",
        }
    ]


@pytest.mark.parametrize(
    "payload,code",
    [
        (
            {
                "dataset_version": "ds-1",
                "data": [
                    {
                        "tenant_id": "tenant-b",
                        "metric_name": "next_day_retention",
                        "stat_date": "2026-11-07",
                        "value": 0.4,
                    }
                ],
            },
            "metric_source_tenant_mismatch",
        ),
        (
            {
                "dataset_version": "ds-1",
                "data": [
                    {
                        "tenant_id": "tenant-a",
                        "metric_name": "revenue_secret",
                        "stat_date": "2026-11-07",
                        "value": 999,
                    }
                ],
            },
            "metric_source_metric_not_allowed",
        ),
    ],
)
def test_metric_source_rejects_cross_tenant_and_unrequested_metrics(payload, code):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )
    adapter = HttpMetricSourceAdapter.from_settings(_settings(), client=client)

    with pytest.raises(MetricSourceError) as excinfo:
        adapter.fetch_snapshots(
            tenant_id="tenant-a",
            metric_names=["next_day_retention"],
            date_from=date(2026, 11, 1),
            date_to=date(2026, 11, 8),
        )

    assert excinfo.value.code == code


def test_metric_source_rejects_response_over_row_limit():
    rows = [
        {
            "tenant_id": "tenant-a",
            "metric_name": "next_day_retention",
            "stat_date": "2026-11-07",
            "value": 0.4,
        },
        {
            "tenant_id": "tenant-a",
            "metric_name": "next_day_retention",
            "stat_date": "2026-11-08",
            "value": 0.41,
        },
    ]
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"dataset_version": "ds-1", "data": rows})
        )
    )
    adapter = HttpMetricSourceAdapter.from_settings(
        _settings(metric_source_http_max_rows=1), client=client
    )

    with pytest.raises(MetricSourceError) as excinfo:
        adapter.fetch_snapshots(
            tenant_id="tenant-a",
            metric_names=["next_day_retention"],
            date_from=date(2026, 11, 1),
            date_to=date(2026, 11, 8),
        )

    assert excinfo.value.code == "metric_source_too_many_rows"


def test_metric_source_rejects_versions_longer_than_database_contract():
    payload = {
        "dataset_version": "d" * 65,
        "source_version": "s" * 65,
        "data": [
            {
                "tenant_id": "tenant-a",
                "metric_name": "next_day_retention",
                "stat_date": "2026-11-07",
                "value": 0.4,
            }
        ],
    }
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )
    adapter = HttpMetricSourceAdapter.from_settings(_settings(), client=client)

    with pytest.raises(MetricSourceError) as excinfo:
        adapter.fetch_snapshots(
            tenant_id="tenant-a",
            metric_names=["next_day_retention"],
            date_from=date(2026, 11, 1),
            date_to=date(2026, 11, 8),
        )

    assert excinfo.value.code == "metric_source_invalid_response"


@pytest.mark.parametrize(
    "record,code",
    [
        (
            {
                "tenant_id": "tenant-a",
                "metric_name": "next_day_retention",
                "stat_date": "2026-10-31",
                "value": 0.4,
            },
            "metric_source_date_out_of_range",
        ),
        (
            {
                "tenant_id": "tenant-a",
                "metric_name": "next_day_retention",
                "stat_date": "not-a-date",
                "value": 0.4,
            },
            "metric_source_invalid_response",
        ),
        (
            {
                "tenant_id": "tenant-a",
                "metric_name": "next_day_retention",
                "stat_date": "2026-11-07",
            },
            "metric_source_invalid_response",
        ),
    ],
)
def test_metric_source_rejects_invalid_or_out_of_window_records(record, code):
    payload = {"dataset_version": "ds-1", "data": [record]}
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )
    adapter = HttpMetricSourceAdapter.from_settings(_settings(), client=client)

    with pytest.raises(MetricSourceError) as excinfo:
        adapter.fetch_snapshots(
            tenant_id="tenant-a",
            metric_names=["next_day_retention"],
            date_from=date(2026, 11, 1),
            date_to=date(2026, 11, 8),
        )

    assert excinfo.value.code == code


def test_sync_imports_standardized_batch_idempotently(session):
    class StubAdapter:
        def fetch_snapshots(self, **_kwargs):
            from app.adapters.metric_http import MetricSourceBatch

            return MetricSourceBatch(
                dataset_version="ds-real-1",
                source_version="warehouse-1",
                request_id="req-1",
                records=[
                    {
                        "metric_name": "next_day_retention",
                        "platform": "android",
                        "stat_date": "2026-11-07",
                        "value": 0.42,
                        "sample_size": 1200,
                        "source_version": "warehouse-1",
                    }
                ],
            )

    settings = _settings()
    first = sync_metric_source(
        session,
        tenant_id="tenant-a",
        settings=settings,
        adapter=StubAdapter(),
        today=date(2026, 11, 8),
    )
    second = sync_metric_source(
        session,
        tenant_id="tenant-a",
        settings=settings,
        adapter=StubAdapter(),
        today=date(2026, 11, 8),
    )

    assert first["status"] == "synced"
    assert first["rows_received"] == 1
    assert first["rows_written"] == 1
    assert second["rows_written"] == 1
    assert session.query(MetricSnapshot).count() == 1
    snapshot = session.query(MetricSnapshot).one()
    assert snapshot.dataset_version == "ds-real-1"
    assert snapshot.source_version == "warehouse-1"


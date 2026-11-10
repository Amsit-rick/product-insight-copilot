"""把只读指标源批次安全导入本地标准化指标快照。"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.metric_http import HttpMetricSourceAdapter
from app.config import Settings
from app.models.base import utcnow
from app.services.metric_import import CANONICAL_COLUMNS, MetricImporter


def _records_csv(records: list[dict]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(CANONICAL_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    return buffer.getvalue().encode("utf-8-sig")


def sync_metric_source(
    session: Session,
    *,
    tenant_id: str,
    settings: Settings,
    adapter: Any | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """读取已批准指标并复用现有导入校验、去重、质量标记与审计。"""
    if not settings.metric_source_http_enabled:
        return {"status": "disabled", "rows_received": 0, "rows_written": 0}

    current_date = today or utcnow().date()
    date_from = current_date - timedelta(days=max(settings.proactive_anomaly_lookback_days - 1, 0))
    owns_adapter = adapter is None
    source = adapter or HttpMetricSourceAdapter.from_settings(settings)
    try:
        batch = source.fetch_snapshots(
            tenant_id=tenant_id,
            metric_names=settings.proactive_anomaly_metrics,
            date_from=date_from,
            date_to=current_date,
        )
        if not batch.records:
            return {
                "status": "no_data",
                "rows_received": 0,
                "rows_written": 0,
                "dataset_version": batch.dataset_version,
            }
        imported = MetricImporter(session).import_bytes(
            tenant_id=tenant_id,
            filename="metric-source-http.csv",
            data=_records_csv(batch.records),
            dataset_version=batch.dataset_version,
        )
        return {
            "status": "synced",
            "rows_received": len(batch.records),
            "rows_written": int(imported["written"]),
            "dataset_version": batch.dataset_version,
            "metric_names": imported["metric_names"],
            "quality_flags": imported["quality_flags"],
        }
    finally:
        if owns_adapter:
            source.close()


__all__ = ["sync_metric_source"]

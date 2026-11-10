"""Default-off, read-only metric anomaly trigger.

The scanner only reads normalized metric snapshots and writes a candidate alert.
It never invokes a model, runs an Agent, dispatches a task, or calls an external system.
Creating a diagnosis task is a separate, explicit human action.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import Settings
from app.metrics.schemas import AnomalyRequest
from app.metrics.service import SqlMetricService
from app.models.base import utcnow
from app.models.identity import User
from app.models.metrics import MetricDefinition, MetricSnapshot
from app.models.workflow import AnalysisTask, OperationalAlert
from app.services.audit import record_audit
from app.services.operational_alerts import acknowledge_alert, raise_operational_alert
from app.services.task_service import TaskService

KIND = "proactive_metric_anomaly"


class ProactiveTriggerDisabled(RuntimeError):
    pass


def trigger_status(settings: Settings) -> dict[str, Any]:
    from app.adapters.metric_http import validate_metric_source_settings

    metric_source_errors = validate_metric_source_settings(settings)
    return {
        "enabled": bool(settings.proactive_anomaly_enabled),
        "read_only_scan": True,
        "metrics": list(settings.proactive_anomaly_metrics),
        "lookback_days": settings.proactive_anomaly_lookback_days,
        "max_data_age_days": settings.proactive_anomaly_max_data_age_days,
        "max_data_age_by_metric": dict(settings.proactive_anomaly_max_data_age_by_metric),
        "method": settings.proactive_anomaly_method,
        "threshold": settings.proactive_anomaly_threshold,
        "automatic_task_dispatch": False,
        "requires_human_confirmation": True,
        "metric_source": {
            "enabled": bool(settings.metric_source_http_enabled),
            "configured": bool(settings.metric_source_http_enabled and not metric_source_errors),
            "upstream_read_only": True,
        },
    }


def max_data_age_for_metric(settings: Settings, metric_name: str) -> int:
    configured = settings.proactive_anomaly_max_data_age_by_metric.get(metric_name)
    if isinstance(configured, int) and not isinstance(configured, bool) and 0 <= configured <= 365:
        return configured
    return settings.proactive_anomaly_max_data_age_days


def _metric_window(session: Session, tenant_id: str, metric_name: str, lookback_days: int):
    max_date = (
        session.query(func.max(MetricSnapshot.stat_date))
        .filter(
            MetricSnapshot.tenant_id.in_([tenant_id, "*"]),
            MetricSnapshot.metric_name == metric_name,
        )
        .scalar()
    )
    if max_date is None:
        return None
    return max_date - timedelta(days=max(lookback_days - 1, 0)), max_date


def _display_name(session: Session, tenant_id: str, metric_name: str) -> str:
    row = (
        session.query(MetricDefinition)
        .filter(
            MetricDefinition.tenant_id.in_([tenant_id, "*"]),
            MetricDefinition.name == metric_name,
        )
        .order_by(MetricDefinition.tenant_id.desc())
        .first()
    )
    return row.display_name if row else metric_name


def _date_text(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def scan_tenant_anomalies(
    session: Session,
    *,
    tenant_id: str,
    settings: Settings,
) -> dict[str, Any]:
    """Run a deterministic read-only scan and create deduplicated candidates."""
    if not settings.proactive_anomaly_enabled:
        raise ProactiveTriggerDisabled("主动异常触发器未启用")

    metric_names = list(dict.fromkeys(settings.proactive_anomaly_metrics))
    service = SqlMetricService(
        session,
        tenant_id=tenant_id,
        roles=["admin"],
        user_id=None,
        clearance=99,
    )
    scanned = 0
    skipped: list[dict[str, Any]] = []
    alert_ids: list[str] = []

    for metric_name in metric_names:
        window = _metric_window(
            session,
            tenant_id,
            metric_name,
            settings.proactive_anomaly_lookback_days,
        )
        if window is None:
            skipped.append({"metric_name": metric_name, "reason": "no_metric_data"})
            continue
        start_date, end_date = window
        age_days = (utcnow().date() - end_date).days
        max_age_days = max_data_age_for_metric(settings, metric_name)
        if age_days < 0:
            skipped.append(
                {
                    "metric_name": metric_name,
                    "reason": "future_metric_data",
                    "latest_stat_date": end_date.isoformat(),
                    "age_days": age_days,
                    "max_age_days": max_age_days,
                }
            )
            continue
        if age_days > max_age_days:
            skipped.append(
                {
                    "metric_name": metric_name,
                    "reason": "stale_metric_data",
                    "latest_stat_date": end_date.isoformat(),
                    "age_days": age_days,
                    "max_age_days": max_age_days,
                }
            )
            continue
        result = service.detect_anomaly(
            AnomalyRequest(
                metric_name=metric_name,
                start_date=start_date,
                end_date=end_date,
                method=settings.proactive_anomaly_method,
                threshold=settings.proactive_anomaly_threshold,
            )
        )
        scanned += 1
        if result.summary.get("policy_denied"):
            skipped.append({"metric_name": metric_name, "reason": "metric_access_denied"})
            continue
        display_name = _display_name(session, tenant_id, metric_name)
        for anomaly in result.rows:
            stat_date = _date_text(anomaly.get("stat_date"))
            direction = str(anomaly.get("direction") or "unknown")
            value = anomaly.get("value")
            alert = raise_operational_alert(
                session,
                tenant_id=tenant_id,
                kind=KIND,
                severity="warning",
                title=f"发现候选指标异常：{display_name}",
                message=(
                    f"{display_name}在 {stat_date} 出现{('下降' if direction == 'drop' else '上升')}异常"
                    f"（值 {value}）。当前仅完成只读扫描，尚未启动诊断，请人工确认。"
                ),
                fingerprint=f"proactive:metric:{metric_name}:{stat_date}:{direction}",
                source_type="metric_snapshot",
                source_id=metric_name,
                metadata={
                    "read_only_scan": True,
                    "metric_name": metric_name,
                    "metric_display_name": display_name,
                    "stat_date": stat_date,
                    "direction": direction,
                    "value": value,
                    "zscore": anomaly.get("zscore"),
                    "bound": anomaly.get("bound"),
                    "date_from": start_date.isoformat(),
                    "date_to": end_date.isoformat(),
                    "method": settings.proactive_anomaly_method,
                    "threshold": settings.proactive_anomaly_threshold,
                    "dataset_version": result.dataset_version,
                    "source_version": result.source_version,
                    "quality_flags": result.quality_flags,
                    "warnings": result.warnings,
                    "diagnosis_task_id": None,
                },
            )
            alert_ids.append(alert.id)
            record_audit(
                session,
                action="proactive_anomaly_candidate_detected",
                tenant_id=tenant_id,
                actor_name="system",
                resource_type="operational_alert",
                resource_id=alert.id,
                detail={
                    "metric_name": metric_name,
                    "stat_date": stat_date,
                    "direction": direction,
                    "read_only_scan": True,
                    "diagnosis_dispatched": False,
                },
            )

    return {
        **trigger_status(settings),
        "read_only": True,
        "metrics_scanned": scanned,
        "anomalies_found": len(alert_ids),
        "alert_ids": alert_ids,
        "skipped": skipped,
    }


def create_diagnosis_for_alert(
    session: Session,
    *,
    alert: OperationalAlert,
    user: User,
) -> tuple[AnalysisTask, bool]:
    """Convert one candidate into a diagnosis after explicit human confirmation."""
    if alert.kind != KIND:
        raise ValueError("该告警不是主动异常候选")
    metadata = dict(alert.metadata_json or {})
    existing_id = metadata.get("diagnosis_task_id")
    if existing_id:
        existing = session.get(AnalysisTask, str(existing_id))
        if existing is not None:
            return existing, False

    metric_name = str(metadata.get("metric_name") or alert.source_id or "")
    display_name = str(metadata.get("metric_display_name") or metric_name)
    stat_date = str(metadata.get("stat_date") or "未知日期")
    direction = str(metadata.get("direction") or "unknown")
    direction_text = "下降" if direction == "drop" else "上升"
    query = f"{display_name}在 {stat_date} 出现{direction_text}异常，请核对指标质量、版本、反馈与研发证据并给出归因。"
    filters = {
        "metrics": [metric_name] if metric_name else [],
        "date_from": metadata.get("date_from"),
        "date_to": metadata.get("date_to"),
        "force_human_review": True,
        "proactive_trigger": {
            "alert_id": alert.id,
            "kind": alert.kind,
            "read_only_scan": True,
            "confirmed_by": user.id,
        },
    }
    task, created = TaskService(session).create(
        user=user,
        query=query,
        title=f"主动异常诊断：{display_name} {stat_date}",
        filters=filters,
        idempotency_key=f"proactive-alert:{alert.id}",
        force_human_review=True,
    )
    metadata["diagnosis_task_id"] = task.id
    metadata["confirmed_by"] = user.id
    metadata["confirmed_by_name"] = user.username
    alert.metadata_json = metadata
    if alert.status != "acknowledged":
        acknowledge_alert(session, alert, actor_id=user.id)
    record_audit(
        session,
        action="proactive_anomaly_diagnosis_confirmed",
        tenant_id=alert.tenant_id,
        actor_id=user.id,
        actor_name=user.username,
        resource_type="analysis_task",
        resource_id=task.id,
        detail={"operational_alert_id": alert.id, "created": created},
    )
    session.flush()
    return task, created


__all__ = [
    "KIND",
    "ProactiveTriggerDisabled",
    "create_diagnosis_for_alert",
    "scan_tenant_anomalies",
    "trigger_status",
]

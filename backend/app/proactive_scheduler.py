"""Singleton scheduler for the default-off read-only anomaly scanner."""

from __future__ import annotations

import argparse
import signal
import socket
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db.session import SessionLocal, engine
from app.logging_utils import get_logger
from app.models.identity import Tenant
from app.services.audit import record_audit
from app.services.proactive_anomaly import scan_tenant_anomalies
from app.services.metric_source_sync import sync_metric_source
from app.services.scheduler_lease import SqlSchedulerLease

logger = get_logger(__name__)

LEASE_NAME = "proactive-anomaly"
Scanner = Callable[..., dict[str, Any]]
MetricSyncer = Callable[..., dict[str, Any]]


class MetricSourceSyncFailure(RuntimeError):
    pass


def default_scheduler_id() -> str:
    return f"{socket.gethostname()}-{__import__('os').getpid()}"


class ProactiveAnomalyScheduler:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: sessionmaker[Session] | None = None,
        lease: SqlSchedulerLease | None = None,
        owner_id: str | None = None,
        metric_syncer: MetricSyncer = sync_metric_source,
        scanner: Scanner = scan_tenant_anomalies,
    ) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory or SessionLocal
        self.lease = lease or SqlSchedulerLease(self.session_factory)
        self.owner_id = owner_id or default_scheduler_id()
        self.metric_syncer = metric_syncer
        self.scanner = scanner
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    def _active_tenant_ids(self) -> list[str]:
        session = self.session_factory()
        try:
            return [
                row[0]
                for row in session.query(Tenant.id)
                .filter(Tenant.status == "active")
                .order_by(Tenant.code.asc())
                .all()
            ]
        finally:
            session.close()

    def _scan_tenant(self, tenant_id: str) -> dict[str, Any]:
        session = self.session_factory()
        try:
            source_result = None
            if self.settings.metric_source_http_enabled:
                try:
                    source_result = self.metric_syncer(
                        session,
                        tenant_id=tenant_id,
                        settings=self.settings,
                    )
                except Exception as exc:
                    raise MetricSourceSyncFailure(type(exc).__name__) from exc
            result = self.scanner(
                session,
                tenant_id=tenant_id,
                settings=self.settings,
            )
            record_audit(
                session,
                action="proactive_anomaly_scheduled_scan",
                tenant_id=tenant_id,
                actor_name="proactive-scheduler",
                resource_type="proactive_anomaly_trigger",
                outcome="success",
                detail={
                    "read_only": True,
                    "metrics_scanned": int(result.get("metrics_scanned", 0)),
                    "metrics_skipped": len(result.get("skipped") or []),
                    "anomalies_found": int(result.get("anomalies_found", 0)),
                    "metric_source_status": (source_result or {}).get("status", "disabled"),
                    "source_rows_received": int((source_result or {}).get("rows_received", 0)),
                    "source_rows_written": int((source_result or {}).get("rows_written", 0)),
                },
            )
            session.commit()
            result["metric_source_sync"] = source_result or {
                "status": "disabled",
                "rows_received": 0,
                "rows_written": 0,
            }
            return result
        except Exception as exc:
            session.rollback()
            record_audit(
                session,
                action="proactive_anomaly_scheduled_scan",
                tenant_id=tenant_id,
                actor_name="proactive-scheduler",
                resource_type="proactive_anomaly_trigger",
                outcome="failure",
                detail={"read_only": True, "error_type": type(exc).__name__},
            )
            session.commit()
            raise
        finally:
            session.close()

    def run_once(self) -> dict[str, Any]:
        if not self.settings.proactive_anomaly_enabled:
            return {"status": "disabled", "read_only": True}

        lease_seconds = self.settings.proactive_anomaly_lease_seconds
        if not self.lease.acquire(LEASE_NAME, self.owner_id, lease_seconds=lease_seconds):
            return {"status": "skipped_locked", "read_only": True}

        result: dict[str, Any] = {
            "status": "completed",
            "read_only": True,
            "tenants_total": 0,
            "tenants_succeeded": 0,
            "tenants_failed": 0,
            "metrics_scanned": 0,
            "metrics_skipped": 0,
            "skipped_reasons": {},
            "anomalies_found": 0,
            "source_rows_received": 0,
            "source_rows_written": 0,
            "source_sync_failures": 0,
            "errors": [],
        }
        try:
            tenant_ids = self._active_tenant_ids()
            result["tenants_total"] = len(tenant_ids)
            for tenant_id in tenant_ids:
                if self._stopping:
                    result["status"] = "stopped"
                    break
                if not self.lease.renew(
                    LEASE_NAME,
                    self.owner_id,
                    lease_seconds=lease_seconds,
                ):
                    result["status"] = "lease_lost"
                    break
                try:
                    tenant_result = self._scan_tenant(tenant_id)
                except Exception as exc:
                    result["tenants_failed"] += 1
                    if isinstance(exc, MetricSourceSyncFailure):
                        result["source_sync_failures"] += 1
                    result["errors"].append(
                        {"tenant_id": tenant_id, "error_type": type(exc).__name__}
                    )
                    logger.exception("主动异常扫描失败 tenant=%s", tenant_id)
                    continue
                result["tenants_succeeded"] += 1
                result["metrics_scanned"] += int(tenant_result.get("metrics_scanned", 0))
                result["anomalies_found"] += int(tenant_result.get("anomalies_found", 0))
                source_sync = tenant_result.get("metric_source_sync") or {}
                result["source_rows_received"] += int(source_sync.get("rows_received", 0))
                result["source_rows_written"] += int(source_sync.get("rows_written", 0))
                skipped = tenant_result.get("skipped") or []
                if isinstance(skipped, list):
                    result["metrics_skipped"] += len(skipped)
                    for item in skipped:
                        if not isinstance(item, dict):
                            continue
                        reason = str(item.get("reason") or "unknown")
                        result["skipped_reasons"][reason] = (
                            int(result["skipped_reasons"].get(reason, 0)) + 1
                        )

            if result["status"] == "completed" and result["tenants_failed"]:
                result["status"] = "partial"
            error = None
            if result["errors"]:
                error = f"{result['tenants_failed']} tenant scan(s) failed"
            self.lease.record_result(
                LEASE_NAME,
                self.owner_id,
                status=str(result["status"]),
                result=result,
                error=error,
            )
            return result
        finally:
            self.lease.release(LEASE_NAME, self.owner_id)

    def run_forever(self, *, max_rounds: int | None = None) -> None:
        rounds = 0
        interval = self.settings.proactive_anomaly_interval_seconds
        while not self._stopping and (max_rounds is None or rounds < max_rounds):
            started = time.monotonic()
            outcome = self.run_once()
            logger.info("主动异常调度轮次完成: %s", outcome)
            rounds += 1
            remaining = max(0.0, interval - (time.monotonic() - started))
            deadline = time.monotonic() + remaining
            while not self._stopping and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))


def scheduler_schema_ready() -> bool:
    return inspect(engine).has_table("scheduler_leases")


def wait_for_scheduler_schema(
    *, timeout_seconds: float = 60.0, poll_interval_seconds: float = 1.0
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not scheduler_schema_ready():
        if time.monotonic() >= deadline:
            raise RuntimeError("scheduler_leases 表未就绪，请先执行数据库迁移")
        time.sleep(poll_interval_seconds)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读主动异常单实例调度器")
    parser.add_argument("--once", action="store_true", help="只执行一轮后退出")
    parser.add_argument("--rounds", type=int, default=None, help="最多执行轮数")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    if not settings.proactive_anomaly_enabled:
        logger.info("只读主动异常触发器默认关闭，调度器不启动")
        return 0
    wait_for_scheduler_schema()
    scheduler = ProactiveAnomalyScheduler(settings=settings)

    def _handle_signal(signum, _frame):  # pragma: no cover
        logger.info("收到信号 %s，主动异常调度器准备退出", signum)
        scheduler.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    if args.once:
        logger.info("主动异常调度结果: %s", scheduler.run_once())
        return 0
    scheduler.run_forever(max_rounds=args.rounds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

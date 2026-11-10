"""运行告警 Webhook 投递：持久队列、签名、退避重试与死信。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from app.config import Settings, get_settings
from app.db.session import SessionLocal, write_scope
from app.logging_utils import get_logger
from app.models.base import utcnow
from app.models.workflow import OperationalAlert, OperationalAlertDelivery

logger = get_logger(__name__)

PENDING = "pending"
CLAIMED = "claimed"
SENT = "sent"
DEAD = "dead"


def default_delivery_worker_id() -> str:
    return f"alert-{socket.gethostname()}-{os.getpid()}"


@dataclass(frozen=True)
class ClaimedDelivery:
    id: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    worker_id: str


class DeliveryAttemptError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


def _delivery_payload(alert: OperationalAlert) -> dict[str, Any]:
    return {
        "event": "operational_alert",
        "version": 1,
        "alert": {
            "id": alert.id,
            "tenant_id": alert.tenant_id,
            "kind": alert.kind,
            "severity": alert.severity,
            "title": alert.title,
            "message": alert.message,
            "status": alert.status,
            "occurrence_count": alert.occurrence_count,
            "source_type": alert.source_type,
            "source_id": alert.source_id,
            "last_occurred_at": alert.last_occurred_at.isoformat(),
        },
    }


def enqueue_alert_webhook(
    session,
    alert: OperationalAlert,
    *,
    settings: Settings | None = None,
) -> OperationalAlertDelivery | None:
    """在当前事务内写入 outbox；未启用时不产生任何外部投递记录。"""
    settings = settings or get_settings()
    if not settings.alert_webhook_enabled or not settings.alert_webhook_url.strip():
        return None
    existing = (
        session.query(OperationalAlertDelivery)
        .filter(
            OperationalAlertDelivery.alert_id == alert.id,
            OperationalAlertDelivery.occurrence_no == alert.occurrence_count,
            OperationalAlertDelivery.channel == "webhook",
        )
        .one_or_none()
    )
    if existing is not None:
        return existing
    try:
        with session.begin_nested():
            row = OperationalAlertDelivery(
                alert_id=alert.id,
                tenant_id=alert.tenant_id,
                channel="webhook",
                occurrence_no=alert.occurrence_count,
                status=PENDING,
                payload=_delivery_payload(alert),
                attempts=0,
                max_attempts=settings.alert_webhook_max_attempts,
                visible_at=utcnow(),
            )
            session.add(row)
            session.flush()
        return row
    except IntegrityError:
        return (
            session.query(OperationalAlertDelivery)
            .filter(
                OperationalAlertDelivery.alert_id == alert.id,
                OperationalAlertDelivery.occurrence_no == alert.occurrence_count,
                OperationalAlertDelivery.channel == "webhook",
            )
            .one()
        )


class SqlAlertDeliveryQueue:
    def __init__(self, settings: Settings | None = None, session_factory: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._session_factory = session_factory

    def _write(self):
        return write_scope(self._session_factory)

    def _read_session(self):
        return (self._session_factory or SessionLocal)()

    def reclaim_expired(self) -> int:
        now = utcnow()
        with self._write() as session:
            rows = (
                session.query(OperationalAlertDelivery)
                .filter(
                    OperationalAlertDelivery.status == CLAIMED,
                    OperationalAlertDelivery.lease_expires_at.isnot(None),
                    OperationalAlertDelivery.lease_expires_at < now,
                )
                .all()
            )
            for row in rows:
                row.claimed_by = None
                row.claimed_at = None
                row.lease_expires_at = None
                if row.attempts >= row.max_attempts:
                    row.status = DEAD
                    row.last_error = "投递 worker 租约过期且已达到最大尝试次数"
                else:
                    row.status = PENDING
                    row.visible_at = now
                    row.last_error = "投递 worker 未在租约内确认，已回收"
            return len(rows)

    def claim(self, worker_id: str, *, limit: int = 10) -> list[ClaimedDelivery]:
        now = utcnow()
        read = self._read_session()
        try:
            candidates = [
                row[0]
                for row in read.query(OperationalAlertDelivery.id)
                .filter(
                    OperationalAlertDelivery.status == PENDING,
                    or_(
                        OperationalAlertDelivery.visible_at.is_(None),
                        OperationalAlertDelivery.visible_at <= now,
                    ),
                    OperationalAlertDelivery.attempts < OperationalAlertDelivery.max_attempts,
                )
                .order_by(OperationalAlertDelivery.created_at.asc())
                .limit(max(1, limit) * 3)
                .all()
            ]
        finally:
            read.close()

        claimed: list[ClaimedDelivery] = []
        for delivery_id in candidates:
            if len(claimed) >= limit:
                break
            with self._write() as session:
                updated = (
                    session.query(OperationalAlertDelivery)
                    .filter(
                        OperationalAlertDelivery.id == delivery_id,
                        OperationalAlertDelivery.status == PENDING,
                    )
                    .update(
                        {
                            OperationalAlertDelivery.status: CLAIMED,
                            OperationalAlertDelivery.claimed_by: worker_id,
                            OperationalAlertDelivery.claimed_at: now,
                            OperationalAlertDelivery.lease_expires_at: now
                            + timedelta(seconds=self.settings.alert_webhook_lease_seconds),
                            OperationalAlertDelivery.attempts: OperationalAlertDelivery.attempts
                            + 1,
                        },
                        synchronize_session=False,
                    )
                )
                if updated != 1:
                    continue
                row = session.get(OperationalAlertDelivery, delivery_id)
                claimed.append(
                    ClaimedDelivery(
                        id=row.id,
                        payload=dict(row.payload or {}),
                        attempts=row.attempts,
                        max_attempts=row.max_attempts,
                        worker_id=worker_id,
                    )
                )
        return claimed

    def ack(self, delivery_id: str, worker_id: str, status_code: int) -> bool:
        with self._write() as session:
            updated = (
                session.query(OperationalAlertDelivery)
                .filter(
                    OperationalAlertDelivery.id == delivery_id,
                    OperationalAlertDelivery.status == CLAIMED,
                    OperationalAlertDelivery.claimed_by == worker_id,
                )
                .update(
                    {
                        OperationalAlertDelivery.status: SENT,
                        OperationalAlertDelivery.last_status_code: status_code,
                        OperationalAlertDelivery.last_error: None,
                        OperationalAlertDelivery.delivered_at: utcnow(),
                        OperationalAlertDelivery.claimed_by: None,
                        OperationalAlertDelivery.claimed_at: None,
                        OperationalAlertDelivery.lease_expires_at: None,
                    },
                    synchronize_session=False,
                )
            )
            return updated == 1

    def nack(
        self,
        delivery_id: str,
        worker_id: str,
        error: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> str:
        with self._write() as session:
            row = session.get(OperationalAlertDelivery, delivery_id)
            if row is None or row.status != CLAIMED or row.claimed_by != worker_id:
                return "ignored"
            row.last_error = str(error)[:1000]
            row.last_status_code = status_code
            row.claimed_by = None
            row.claimed_at = None
            row.lease_expires_at = None
            if not retryable or row.attempts >= row.max_attempts:
                row.status = DEAD
                return DEAD
            delay = min(
                3600.0,
                self.settings.alert_webhook_retry_base_seconds
                * (2 ** max(0, row.attempts - 1)),
            )
            row.status = PENDING
            row.visible_at = utcnow() + timedelta(seconds=delay)
            return PENDING

    def stats(self, *, tenant_id: str | None = None) -> dict[str, int]:
        session = self._read_session()
        try:
            query = session.query(
                OperationalAlertDelivery.status,
                func.count(OperationalAlertDelivery.id),
            )
            if tenant_id is not None:
                query = query.filter(OperationalAlertDelivery.tenant_id == tenant_id)
            grouped = dict(query.group_by(OperationalAlertDelivery.status).all())
            return {name: int(grouped.get(name, 0)) for name in (PENDING, CLAIMED, SENT, DEAD)}
        finally:
            session.close()

    def list_items(
        self,
        *,
        tenant_id: str,
        status_filter: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        session = self._read_session()
        try:
            query = session.query(OperationalAlertDelivery).filter(
                OperationalAlertDelivery.tenant_id == tenant_id
            )
            if status_filter:
                query = query.filter(OperationalAlertDelivery.status == status_filter)
            rows = query.order_by(OperationalAlertDelivery.created_at.desc()).limit(limit).all()
            return [delivery_payload(row) for row in rows]
        finally:
            session.close()

    def replay(self, delivery_id: str, *, tenant_id: str) -> dict | None:
        with self._write() as session:
            row = (
                session.query(OperationalAlertDelivery)
                .filter(
                    OperationalAlertDelivery.id == delivery_id,
                    OperationalAlertDelivery.tenant_id == tenant_id,
                )
                .one_or_none()
            )
            if row is None:
                return None
            if row.status != DEAD:
                raise ValueError("only dead delivery can be replayed")
            row.status = PENDING
            row.attempts = 0
            row.visible_at = utcnow()
            row.claimed_by = None
            row.claimed_at = None
            row.lease_expires_at = None
            row.delivered_at = None
            session.flush()
            return delivery_payload(row)


def delivery_payload(row: OperationalAlertDelivery) -> dict[str, Any]:
    return {
        "id": row.id,
        "alert_id": row.alert_id,
        "tenant_id": row.tenant_id,
        "channel": row.channel,
        "occurrence_no": row.occurrence_no,
        "status": row.status,
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "visible_at": row.visible_at.isoformat() if row.visible_at else None,
        "last_error": row.last_error,
        "last_status_code": row.last_status_code,
        "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


class AlertDeliveryWorker:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: Any | None = None,
        client: httpx.Client | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.queue = SqlAlertDeliveryQueue(self.settings, session_factory=session_factory)
        self.worker_id = worker_id or default_delivery_worker_id()
        self.client = client or httpx.Client(
            timeout=self.settings.alert_webhook_timeout_seconds,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._stop = threading.Event()

    def _send(self, item: ClaimedDelivery) -> int:
        body = json.dumps(item.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Alert-Delivery-Id": item.id,
            "X-Alert-Timestamp": timestamp,
        }
        secret = self.settings.alert_webhook_secret
        if secret:
            digest = hmac.new(
                secret.encode("utf-8"),
                timestamp.encode("utf-8") + b"." + body,
                hashlib.sha256,
            ).hexdigest()
            headers["X-Alert-Signature"] = f"sha256={digest}"
        try:
            response = self.client.post(
                self.settings.alert_webhook_url,
                content=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise DeliveryAttemptError(type(exc).__name__, retryable=True) from exc
        if 200 <= response.status_code < 300:
            return response.status_code
        retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
        raise DeliveryAttemptError(
            f"HTTP {response.status_code}",
            retryable=retryable,
            status_code=response.status_code,
        )

    def run_once(self) -> int:
        self.queue.reclaim_expired()
        items = self.queue.claim(self.worker_id, limit=10)
        for item in items:
            try:
                status_code = self._send(item)
                self.queue.ack(item.id, self.worker_id, status_code)
            except DeliveryAttemptError as exc:
                state = self.queue.nack(
                    item.id,
                    self.worker_id,
                    str(exc),
                    retryable=exc.retryable,
                    status_code=exc.status_code,
                )
                logger.warning("告警 Webhook 投递失败 delivery=%s state=%s error=%s", item.id, state, exc)
        return len(items)

    def run_forever(self) -> None:
        logger.info("告警 Webhook 投递 worker 已启动：%s", self.worker_id)
        while not self._stop.is_set():
            handled = self.run_once()
            if handled == 0:
                self._stop.wait(self.settings.alert_webhook_poll_interval_seconds)
        logger.info("告警 Webhook 投递 worker 已停止")

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.run_forever,
            name="alert-delivery-worker",
            daemon=True,
        )
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
        if self._owns_client:
            self.client.close()


_worker: AlertDeliveryWorker | None = None
_worker_lock = threading.Lock()


def start_alert_delivery_worker(settings: Settings | None = None):
    global _worker
    settings = settings or get_settings()
    if not settings.alert_webhook_enabled or not settings.alert_webhook_url.strip():
        return None
    with _worker_lock:
        if _worker is None:
            _worker = AlertDeliveryWorker(settings=settings)
        return _worker.start_background()


def reset_alert_delivery_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.stop()
        _worker = None


__all__ = [
    "CLAIMED",
    "DEAD",
    "PENDING",
    "SENT",
    "AlertDeliveryWorker",
    "ClaimedDelivery",
    "DeliveryAttemptError",
    "SqlAlertDeliveryQueue",
    "delivery_payload",
    "enqueue_alert_webhook",
    "reset_alert_delivery_worker",
    "start_alert_delivery_worker",
]

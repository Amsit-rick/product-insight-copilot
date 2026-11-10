from __future__ import annotations

import hashlib
import hmac

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.models.base import Base
from app.models.workflow import OperationalAlertDelivery
from app.services.alert_delivery import (
    AlertDeliveryWorker,
    SqlAlertDeliveryQueue,
    enqueue_alert_webhook,
)
from app.services.operational_alerts import raise_operational_alert


def _factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _settings(**overrides) -> Settings:
    values = {
        "llm_provider": "stub",
        "alert_webhook_enabled": True,
        "alert_webhook_url": "https://alerts.example.test/hooks/operations",
        "alert_webhook_secret": "test-signing-secret",
        "alert_webhook_timeout_seconds": 1,
        "alert_webhook_max_attempts": 2,
        "alert_webhook_retry_base_seconds": 0,
        "alert_webhook_lease_seconds": 30,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _alert(session):
    return raise_operational_alert(
        session,
        tenant_id="tenant-a",
        kind="reflection_auto_rollback",
        severity="critical",
        title="反思灰度已自动回滚",
        message="安全硬指标未达标，覆盖比例已自动回滚到 0%。",
        fingerprint="reflection:auto-rollback",
        source_type="reflection_release",
        source_id="release-1",
    )


def test_disabled_webhook_does_not_create_delivery():
    factory = _factory()
    session = factory()
    alert = _alert(session)

    delivery = enqueue_alert_webhook(
        session,
        alert,
        settings=_settings(alert_webhook_enabled=False),
    )

    assert delivery is None
    assert session.query(OperationalAlertDelivery).count() == 0


def test_delivery_is_idempotent_per_alert_occurrence_and_recurrence_enqueues_again():
    factory = _factory()
    session = factory()
    alert = _alert(session)

    first = enqueue_alert_webhook(session, alert, settings=_settings())
    duplicate = enqueue_alert_webhook(session, alert, settings=_settings())
    repeated_alert = _alert(session)
    second = enqueue_alert_webhook(session, repeated_alert, settings=_settings())

    assert first.id == duplicate.id
    assert second.id != first.id
    assert second.occurrence_no == 2
    assert session.query(OperationalAlertDelivery).count() == 2


def test_two_workers_cannot_claim_the_same_delivery():
    factory = _factory()
    session = factory()
    enqueue_alert_webhook(session, _alert(session), settings=_settings())
    session.commit()
    queue = SqlAlertDeliveryQueue(_settings(), session_factory=factory)

    first = queue.claim("worker-a", limit=1)
    second = queue.claim("worker-b", limit=1)

    assert len(first) == 1
    assert second == []


def test_successful_delivery_is_signed_and_marked_sent():
    factory = _factory()
    session = factory()
    enqueue_alert_webhook(session, _alert(session), settings=_settings())
    session.commit()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    worker = AlertDeliveryWorker(
        settings=_settings(),
        session_factory=factory,
        client=client,
        worker_id="delivery-worker",
    )

    assert worker.run_once() == 1

    check = factory()
    delivery = check.query(OperationalAlertDelivery).one()
    request = captured["request"]
    timestamp = request.headers["X-Alert-Timestamp"]
    expected = hmac.new(
        b"test-signing-secret",
        timestamp.encode("utf-8") + b"." + request.content,
        hashlib.sha256,
    ).hexdigest()
    assert delivery.status == "sent"
    assert delivery.delivered_at is not None
    assert request.headers["X-Alert-Signature"] == f"sha256={expected}"
    assert "test-signing-secret" not in request.content.decode("utf-8")


def test_retryable_failure_retries_then_moves_to_dead_letter():
    factory = _factory()
    session = factory()
    enqueue_alert_webhook(session, _alert(session), settings=_settings())
    session.commit()
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503, text="unavailable"))
    )
    worker = AlertDeliveryWorker(
        settings=_settings(),
        session_factory=factory,
        client=client,
        worker_id="delivery-worker",
    )

    assert worker.run_once() == 1
    assert worker.run_once() == 1

    check = factory()
    delivery = check.query(OperationalAlertDelivery).one()
    assert delivery.status == "dead"
    assert delivery.attempts == 2
    assert "HTTP 503" in delivery.last_error


def test_non_retryable_http_failure_moves_directly_to_dead_letter():
    factory = _factory()
    session = factory()
    enqueue_alert_webhook(session, _alert(session), settings=_settings())
    session.commit()
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(400, text="bad payload"))
    )
    worker = AlertDeliveryWorker(
        settings=_settings(),
        session_factory=factory,
        client=client,
        worker_id="delivery-worker",
    )

    assert worker.run_once() == 1

    check = factory()
    delivery = check.query(OperationalAlertDelivery).one()
    assert delivery.status == "dead"
    assert delivery.attempts == 1
    assert "HTTP 400" in delivery.last_error

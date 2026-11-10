"""Safe Webhook delivery probes with a deterministic loopback contract drill."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import urlparse

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

SCHEMA_VERSION = "webhook-integration-probe-v1"
_LOOPBACK_SECRET = "local-contract-signing-secret"


def inspect_webhook_configuration(settings: Settings) -> dict[str, Any]:
    """Return only non-sensitive readiness metadata for the configured receiver."""
    parsed = urlparse(settings.alert_webhook_url.strip())
    checks = {
        "enabled": settings.alert_webhook_enabled,
        "url_configured": bool(settings.alert_webhook_url.strip()),
        "https": parsed.scheme.lower() == "https",
        "host_present": bool(parsed.hostname),
        "credentials_absent": parsed.username is None and parsed.password is None,
        "fragment_absent": not parsed.fragment,
        "hmac_secret_configured": bool(settings.alert_webhook_secret),
    }
    return {"passed": all(checks.values()), "checks": checks}


class _ReceiverState:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.retry_calls = 0
        self.dead_accept = False

    def handle(self, path: str, headers, body: bytes) -> int:
        timestamp = headers.get("X-Alert-Timestamp", "")
        expected = hmac.new(
            self.secret.encode("utf-8"),
            timestamp.encode("utf-8") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        signature_valid = hmac.compare_digest(
            headers.get("X-Alert-Signature", ""), f"sha256={expected}"
        )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        payload_valid = (
            isinstance(payload, dict)
            and payload.get("event") == "operational_alert"
            and isinstance(payload.get("alert"), dict)
        )
        with self.lock:
            if path == "/success":
                status = 204
            elif path == "/retry":
                self.retry_calls += 1
                status = 503 if self.retry_calls == 1 else 204
            elif path == "/dead":
                status = 204 if self.dead_accept else 400
            else:
                status = 404
            self.requests.append(
                {
                    "path": path,
                    "status": status,
                    "signature_valid": signature_valid,
                    "payload_valid": payload_valid,
                    "delivery_id_present": bool(headers.get("X-Alert-Delivery-Id")),
                }
            )
            return status


class _ProbeHandler(BaseHTTPRequestHandler):
    state: _ReceiverState

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        status = self.state.handle(self.path, self.headers, body)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _loopback_receiver() -> Iterator[tuple[str, _ReceiverState]]:
    state = _ReceiverState(_LOOPBACK_SECRET)
    handler = type("WebhookProbeHandler", (_ProbeHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, name="webhook-probe", daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _probe_settings(url: str, *, max_attempts: int = 2) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        llm_provider="stub",
        alert_webhook_enabled=True,
        alert_webhook_url=url,
        alert_webhook_secret=_LOOPBACK_SECRET,
        alert_webhook_timeout_seconds=2,
        alert_webhook_max_attempts=max_attempts,
        alert_webhook_retry_base_seconds=0,
        alert_webhook_poll_interval_seconds=0.1,
        alert_webhook_lease_seconds=30,
    )


def _enqueue_probe(factory, settings: Settings, *, kind: str) -> str:
    session = factory()
    try:
        alert = raise_operational_alert(
            session,
            tenant_id="webhook-probe-tenant",
            kind=kind,
            severity="warning",
            title="Webhook delivery contract probe",
            message="Synthetic probe event; no production data.",
            fingerprint=f"webhook-probe:{kind}",
            source_type="integration_probe",
            source_id=kind,
        )
        delivery = enqueue_alert_webhook(session, alert, settings=settings)
        session.commit()
        if delivery is None:
            raise RuntimeError("probe delivery was not enqueued")
        return delivery.id
    finally:
        session.close()


def _delivery(factory, delivery_id: str) -> dict[str, Any]:
    session = factory()
    try:
        row = session.get(OperationalAlertDelivery, delivery_id)
        if row is None:
            raise RuntimeError("probe delivery disappeared")
        return {
            "status": row.status,
            "attempts": row.attempts,
            "last_status_code": row.last_status_code,
        }
    finally:
        session.close()


def _run_loopback_scenario(
    base_url: str,
    *,
    path: str,
    kind: str,
    runs: int,
) -> tuple[dict[str, Any], SqlAlertDeliveryQueue]:
    settings = _probe_settings(f"{base_url}{path}")
    factory = _session_factory()
    delivery_id = _enqueue_probe(factory, settings, kind=kind)
    worker = AlertDeliveryWorker(
        settings=settings,
        session_factory=factory,
        worker_id=f"webhook-probe-{kind}",
    )
    try:
        handled = [worker.run_once() for _ in range(runs)]
        result = _delivery(factory, delivery_id)
        result.update({"delivery_id": delivery_id, "handled": handled})
        return result, worker.queue
    finally:
        worker.stop()


def run_local_webhook_contract_drill() -> dict[str, Any]:
    """Exercise the production sender against loopback; never contacts an external host."""
    with _loopback_receiver() as (base_url, receiver):
        success, _ = _run_loopback_scenario(
            base_url, path="/success", kind="success", runs=1
        )
        retry, _ = _run_loopback_scenario(base_url, path="/retry", kind="retry", runs=2)

        settings = _probe_settings(f"{base_url}/dead")
        factory = _session_factory()
        dead_id = _enqueue_probe(factory, settings, kind="dead")
        worker = AlertDeliveryWorker(
            settings=settings,
            session_factory=factory,
            worker_id="webhook-probe-dead",
        )
        try:
            first_handled = worker.run_once()
            before_replay = _delivery(factory, dead_id)
            replayed = worker.queue.replay(dead_id, tenant_id="webhook-probe-tenant")
            after_replay = _delivery(factory, dead_id)
            receiver.dead_accept = True
            second_handled = worker.run_once()
            after_delivery = _delivery(factory, dead_id)
        finally:
            worker.stop()

        by_path: dict[str, list[dict[str, Any]]] = {}
        for request in receiver.requests:
            by_path.setdefault(request["path"], []).append(request)
        all_requests_valid = bool(receiver.requests) and all(
            request["signature_valid"]
            and request["payload_valid"]
            and request["delivery_id_present"]
            for request in receiver.requests
        )
        checks = {
            "hmac_signature": all_requests_valid,
            "success_2xx": (
                success["handled"] == [1]
                and success["status"] == "sent"
                and success["attempts"] == 1
                and [item["status"] for item in by_path.get("/success", [])] == [204]
            ),
            "retry_503_then_2xx": (
                retry["handled"] == [1, 1]
                and retry["status"] == "sent"
                and retry["attempts"] == 2
                and [item["status"] for item in by_path.get("/retry", [])] == [503, 204]
            ),
            "dead_letter_400": (
                first_handled == 1
                and before_replay == {
                    "status": "dead",
                    "attempts": 1,
                    "last_status_code": 400,
                }
                and [item["status"] for item in by_path.get("/dead", [])][0:1] == [400]
            ),
            "replay_then_2xx": (
                replayed is not None
                and after_replay["status"] == "pending"
                and after_replay["attempts"] == 0
                and second_handled == 1
                and after_delivery["status"] == "sent"
                and after_delivery["attempts"] == 1
                and [item["status"] for item in by_path.get("/dead", [])] == [400, 204]
            ),
        }
    return {
        "executed": True,
        "passed": all(checks.values()),
        "transport": "loopback_http",
        "external_network_used": False,
        "checks": checks,
    }


def _run_external_probe(
    settings: Settings,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    probe_settings = settings.model_copy(
        update={"alert_webhook_max_attempts": 1, "alert_webhook_retry_base_seconds": 0}
    )
    factory = _session_factory()
    delivery_id = _enqueue_probe(factory, probe_settings, kind="external_delivery")
    worker = AlertDeliveryWorker(
        settings=probe_settings,
        session_factory=factory,
        client=client,
        worker_id="webhook-real-probe",
    )
    started = time.perf_counter()
    try:
        handled = worker.run_once()
        state = _delivery(factory, delivery_id)
    except Exception as exc:  # Provider errors can contain URLs or headers; retain type only.
        return {
            "executed": True,
            "passed": False,
            "failure_type": type(exc).__name__,
        }
    finally:
        worker.stop()
    return {
        "executed": True,
        "passed": handled == 1 and state["status"] == "sent",
        "delivery_status": state["status"],
        "status_code": state["last_status_code"],
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


def run_webhook_integration_probe(
    settings: Settings,
    *,
    allow_real_call: bool = False,
    external_client: httpx.Client | None = None,
) -> dict[str, Any]:
    configuration = inspect_webhook_configuration(settings)
    local_contract = run_local_webhook_contract_drill()
    if not allow_real_call:
        external_delivery = {
            "executed": False,
            "passed": False,
            "reason": "real_call_not_authorized",
        }
    elif not configuration["passed"]:
        external_delivery = {
            "executed": False,
            "passed": False,
            "reason": "configuration_not_ready",
        }
    else:
        external_delivery = _run_external_probe(settings, client=external_client)
    production_ready = bool(
        configuration["passed"]
        and local_contract["passed"]
        and external_delivery["executed"]
        and external_delivery["passed"]
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "configuration": configuration,
        "local_contract": local_contract,
        "external_delivery": external_delivery,
        "production_ready": production_ready,
    }


__all__ = [
    "SCHEMA_VERSION",
    "inspect_webhook_configuration",
    "run_local_webhook_contract_drill",
    "run_webhook_integration_probe",
]

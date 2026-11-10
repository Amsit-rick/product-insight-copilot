r"""Reproducible, isolated local integration runtime (keeps .env model credentials).

From repository root: .venv\Scripts\python.exe backend\scripts\local_integration.py run
Ctrl+C stops only the child processes created by this launcher. Data survives.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import Settings  # noqa: E402

DEFAULT_STATE = BACKEND / ".tmp" / "local-integration"
SEMANTIC_STATE = DEFAULT_STATE / "semantic"


def integration_environment(
    state_dir: Path,
    sandbox_port: int,
    settings: Settings | None = None,
    *,
    with_local_models: bool = False,
    model_port: int = 8093,
) -> dict[str, str]:
    """Never modify .env or reuse its business DB/outbox/storage in a rehearsal."""
    settings = settings or Settings()
    if settings.app_env not in {"local", "dev", "test"}:
        raise ValueError("local integration is unavailable in production")
    if not 1 <= sandbox_port <= 65535:
        raise ValueError("invalid sandbox port")
    if not 1 <= model_port <= 65535:
        raise ValueError("invalid model port")
    state = state_dir.resolve()
    # A dedicated directory prevents accidental reuse of the normal business DB.
    db_path = state / "integration.db"
    from sqlalchemy.engine import make_url

    configured = make_url(settings.database_url)
    if configured.get_backend_name() == "sqlite" and configured.database:
        if Path(configured.database).resolve() == db_path:
            raise ValueError("integration database must differ from the configured business DB")
    base = f"http://127.0.0.1:{sandbox_port}"
    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "APP_ENV": "local",
        "APP_NAME": "售后工单工作台（本地联调）",
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "AUTO_INIT_DB": "true",
        "AUTO_SEED": "true",
        "STORAGE_BACKEND": "local",
        "STORAGE_LOCAL_DIR": str(state / "objects"),
        "VECTOR_BACKEND": "fallback",
        "CACHE_BACKEND": "memory",
        "CACHE_SQLITE_PATH": str(state / "cache.db"),
        "TASK_RUN_INLINE": "false",
        "TASK_EXECUTION_BACKEND": "queue",
        "WORKER_EMBEDDED": "false",
        "WORKER_CONCURRENCY": "1",
        "CHECKPOINTER_BACKEND": "sqlite",
        "CHECKPOINTER_SQLITE_PATH": str(state / "checkpoints.db"),
        "REFLECTION_ENABLED": "true",
        "REFLECTION_TICKET_ENABLED": "true",
        "REFLECTION_REPORT_ENABLED": "true",
        "REFLECTION_ROLLOUT_PERCENT": "100",
        "REFLECTION_ROLLOUT_TENANTS": "[]",
        "REFLECTION_ROLLOUT_RISK_LEVELS": "[]",
        "REFLECTION_ROLLOUT_ISSUE_TYPES": "[]",
        "REFLECTION_ROLLOUT_AUTO_GUARD": "true",
        "MAX_REFLECTION_ROUNDS": "1",
        "TICKET_USE_MOCK_ADAPTERS": "false",
        "TICKET_FACT_MCP_ENABLED": "false",
        "TICKET_FACT_ORDER_URL": base + "/orders/{order_id}",
        "TICKET_FACT_PAYMENT_URL": base + "/payments/{order_id}",
        "TICKET_FACT_LOGISTICS_URL": base + "/logistics/{order_id}",
        "TICKET_FACT_AFTERSALE_URL": base + "/aftersales/{order_id}",
        "TICKET_FACT_AUTH_HEADERS": json.dumps({"X-Api-Key": "local-sandbox-key"}),
        "TICKET_FACT_TIMEOUT_SECONDS": "0.4",
        "TICKET_FACT_MAX_RETRIES": "0",
        "SANDBOX_API_KEY": "local-sandbox-key",
        "SANDBOX_WEBHOOK_SECRET": "local-webhook-secret",
        "SANDBOX_DELAY_SECONDS": "0.6",
        "METRIC_SOURCE_HTTP_ENABLED": "true",
        "METRIC_SOURCE_HTTP_URL": base + "/api/v1/metric-snapshots",
        "METRIC_SOURCE_HTTP_AUTH_HEADERS": json.dumps({"X-Api-Key": "local-sandbox-key"}),
        # The seeded local metric dictionary contains these two names. The
        # sandbox's extra conversion_rate contract remains available for its
        # adapter tests but is not silently mapped to order_conversion_rate.
        "PROACTIVE_ANOMALY_METRICS": '["crash_rate","next_day_retention"]',
        # Metrics are synced explicitly; no automatic diagnosis/model calls.
        "PROACTIVE_ANOMALY_ENABLED": "false",
        "ALERT_WEBHOOK_ENABLED": "true",
        "ALERT_WEBHOOK_URL": base + "/webhooks/alerts",
        "ALERT_WEBHOOK_SECRET": "local-webhook-secret",
    })
    if with_local_models:
        model_base = f"http://127.0.0.1:{model_port}"
        env.update({
            "EMBEDDING_PROVIDER": "openai_compatible",
            "EMBEDDING_BASE_URL": model_base + "/v1",
            "EMBEDDING_API_KEY": "local-model-key",
            "EMBEDDING_MODEL": "BAAI/bge-small-zh-v1.5",
            "EMBEDDING_DIM": "512",
            "EMBEDDING_VERSION": "fastembed-bge-small-zh-v1.5-v1",
            "EMBEDDING_FAILURE_MODE": "error",
            "RERANKER_ENDPOINT": model_base + "/rerank",
            "RERANKER_API_KEY": "local-model-key",
            "RERANKER_FAILURE_MODE": "error",
            "LOCAL_MODEL_API_KEY": "local-model-key",
            "LOCAL_EMBEDDING_MODEL": "BAAI/bge-small-zh-v1.5",
            "LOCAL_RERANKER_MODEL": "BAAI/bge-reranker-v2-m3-int8",
            "LOCAL_MODEL_CACHE_DIR": str(BACKEND / ".tmp" / "fastembed-cache"),
            "LOCAL_MODEL_THREADS": "4",
        })
    return env


def _wait_ready(process: subprocess.Popen, url: str, timeout: float = 90) -> None:
    import httpx

    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=1, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("service exited; inspect local integration logs")
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.RequestError:
                pass
            time.sleep(0.2)
    raise RuntimeError("service readiness timeout; inspect local integration logs")


def _terminate_child_tree(process: subprocess.Popen) -> None:
    """Stop only this launcher's process tree, including Windows venv shims."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()


def run_all(args: argparse.Namespace, env: dict[str, str]) -> None:
    active_ports = [args.port, args.sandbox_port]
    if args.with_local_models:
        active_ports.append(args.model_port)
    if len(set(active_ports)) != len(active_ports):
        raise ValueError("API, sandbox and model services need different ports")
    # Fail before starting anything; never stop an unrelated listener.
    for port in active_ports:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=True)
    children: list[subprocess.Popen] = []
    logs = []
    try:
        services = (["models"] if args.with_local_models else []) + ["sandbox", "api", "worker"]
        for name in services:
            log = (state / f"{name}.log").open("a", encoding="utf-8")
            logs.append(log)
            if name == "worker":
                command = [sys.executable, "-m", "app.worker"]
            else:
                app_module = {
                    "models": "local_models.app:app",
                    "api": "app.main:app",
                    "sandbox": "sandbox_services.app:app",
                }[name]
                service_port = {
                    "models": args.model_port,
                    "api": args.port,
                    "sandbox": args.sandbox_port,
                }[name]
                command = [
                    sys.executable, "-m", "uvicorn", app_module,
                    "--host", "127.0.0.1", "--port", str(service_port),
                ]
            child = subprocess.Popen(
                command,
                cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            children.append(child)
            print(f"{name}: PID {child.pid}; log {state / (name + '.log')}", flush=True)
            if name == "models":
                _wait_ready(child, f"http://127.0.0.1:{args.model_port}/health", timeout=600)
            elif name == "sandbox":
                _wait_ready(child, f"http://127.0.0.1:{args.sandbox_port}/health")
            elif name == "api":
                _wait_ready(child, f"http://127.0.0.1:{args.port}/health/ready")
        print(f"Local integration: http://127.0.0.1:{args.port}/#/login", flush=True)
        if args.with_local_models:
            print(
                "Semantic retrieval: real BGE embedding + reranker; strict failure mode; "
                f"isolated state {state}",
                flush=True,
            )
        print("Synthetic facts/metrics; separate persistent DB; model config from .env. Ctrl+C to stop.", flush=True)
        while True:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("a service exited; stopped this launcher's children only")
            time.sleep(0.5)
    finally:
        for child in reversed(children):
            _terminate_child_tree(child)
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        for log in logs:
            log.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("run", "api", "worker", "sandbox", "models"))
    parser.add_argument("--port", type=int, default=8033)
    parser.add_argument("--sandbox-port", type=int, default=8091)
    parser.add_argument("--model-port", type=int, default=8093)
    parser.add_argument("--with-local-models", action="store_true")
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args()
    args.state_dir = args.state_dir or (SEMANTIC_STATE if args.with_local_models else DEFAULT_STATE)
    if args.service == "models":
        args.with_local_models = True
    env = integration_environment(
        args.state_dir,
        args.sandbox_port,
        with_local_models=args.with_local_models,
        model_port=args.model_port,
    )
    if not 1 <= args.port <= 65535:
        parser.error("invalid API port")
    if args.service == "run":
        run_all(args, env)
        return 0
    os.environ.update(env)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    if args.service == "worker":
        from app.worker import main as worker_main

        # Do not forward launcher arguments to the worker's parser.
        sys.argv = [sys.argv[0]]
        worker_main()
    else:
        import uvicorn

        uvicorn.run(
            {
                "api": "app.main:app",
                "sandbox": "sandbox_services.app:app",
                "models": "local_models.app:app",
            }[args.service],
            host="127.0.0.1",
            port={
                "api": args.port,
                "sandbox": args.sandbox_port,
                "models": args.model_port,
            }[args.service],
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)

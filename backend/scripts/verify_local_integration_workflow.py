"""Exercise the main API, HTTP facts, reflection and supervisor approval.

Creates explicitly labelled demo/test tickets in the local integration database.
Never sends a customer reply, issues a refund or calls an enterprise API. Real
model use requires --allow-real-models. Results report samples, not accuracy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from scripts.local_integration import DEFAULT_STATE, integration_environment  # noqa: E402


def verify(port: int, state_dir: Path, sandbox_port: int, allow_real: bool) -> dict:
    checks: list[dict] = []
    report: dict = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "local_sandbox_synthetic_facts", "production_ready": False,
        "checks": checks, "tickets": [],
    }

    def check(name: str, condition: bool, **evidence) -> None:
        checks.append({"name": name, "passed": bool(condition), **evidence})
        print(f"{'PASS' if condition else 'FAIL'} {name}", flush=True)

    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False) as client:
        health = client.get("/api/v1/health").raise_for_status().json()
        if health.get("env") != "local" or "本地联调" not in health.get("app", ""):
            raise ValueError("refusing to mutate a service outside the isolated local integration mode")
        model = health.get("llm") or {}
        if model.get("enabled") and not allow_real:
            raise ValueError("real model configured; explicit --allow-real-models required")
        report["model"] = {"provider": model.get("provider"), "model": model.get("model")}
        check("runtime_http_facts", health["fact_adapter"]["backend"] == "http"
              and not health["fact_adapter"]["is_mock"])
        check("runtime_queue_sqlite", health["execution"]["backend"] == "queue"
              and not health["execution"]["worker_embedded"]
              and health["execution"]["checkpointer"]["backend"] == "sqlite")
        check("runtime_reflection_enabled", health["reflection"]["enabled"]
              and health["reflection"]["auto_guard"])

        headers = {}
        for role in ("ops", "admin", "reviewer"):
            login = client.post("/api/v1/auth/login", json={
                "username": role, "password": f"{role}123",
            }).raise_for_status().json()
            headers[role] = {"Authorization": "Bearer " + login["access_token"]}
        suffix = uuid.uuid4().hex[:10]

        def create(order: str, scenario: str, *, demo: bool = False) -> dict:
            ticket = client.post("/api/v1/tickets", headers=headers["ops"], json={
                "subject": f"[本地联调] {scenario}",
                "content": "商品不想要了，申请退货退款，请说明处理条件和下一步。",
                "order_id": order, "data_origin": "demo" if demo else "test",
                "external_id": f"LOCAL-INTEGRATION-{scenario}-{suffix}",
            }).raise_for_status().json()
            report["tickets"].append({"id": ticket["id"], "scenario": scenario})
            return ticket

        def detail(ticket_id: str) -> dict:
            return client.get(f"/api/v1/tickets/{ticket_id}", headers=headers["admin"]).raise_for_status().json()

        for order, scenario in (("SANDBOX-NOTFOUND-001", "not_found"),
                                ("SANDBOX-AUTH-001", "unauthorized"),
                                ("SANDBOX-BADJSON-001", "invalid_json")):
            ticket = create(order, scenario)
            response = client.post(f"/api/v1/tickets/{ticket['id']}/analyze", headers=headers["ops"])
            current = detail(ticket["id"])
            check(f"{scenario}_stops_without_recommendation", response.status_code == 409
                  and current["recommendation"] is None
                  and not current["ticket"].get("last_analysis_task_id"),
                  http_status=response.status_code)

        # Only logistics times out in this scenario. Order/payment remain usable,
        # so the intended behavior is supervised degradation, not a total stop.
        timeout_ticket = create("SANDBOX-TIMEOUT-001", "timeout")
        response = client.post(
            f"/api/v1/tickets/{timeout_ticket['id']}/analyze", headers=headers["ops"]
        )
        if response.status_code == 202:
            # Facts are collected synchronously before dispatch. Cancel the
            # queued workflow: this check targets degradation recording and
            # must not spend a second set of real-model calls.
            timeout_task_id = response.json()["run_id"]
            client.post(f"/api/v1/tasks/{timeout_task_id}/cancel", headers=headers["ops"])
        current = detail(timeout_ticket["id"])
        logistics = next(fact for fact in current["facts"] if fact["source"] == "logistics")
        check("logistics_timeout_is_recorded_without_fabricating_track", response.status_code == 202
              and logistics["payload"] == {}
              and bool(current["ticket"].get("missing_fields")),
              http_status=response.status_code)

        ticket = create("SANDBOX-OK-001", "refund", demo=True)
        started = time.monotonic()
        analysis = client.post(f"/api/v1/tickets/{ticket['id']}/analyze", headers=headers["ops"])
        analysis.raise_for_status()
        task_id = analysis.json()["run_id"]
        report["task_id"] = task_id
        print(f"WAIT real workflow task {task_id}", flush=True)
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            task = client.get(f"/api/v1/tasks/{task_id}", headers=headers["admin"]).raise_for_status().json()
            if task["status"] in {"completed", "failed", "cancelled", "waiting_human"}:
                break
            time.sleep(1)
        else:
            task = {"status": "timeout"}
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        check("refund_workflow_completed", task["status"] == "completed", status=task["status"])
        current = detail(ticket["id"])
        facts = current.get("facts") or []
        check("four_http_fact_sources_persisted", len(facts) == 4
              and all(not fact["is_mock"] for fact in facts), source_count=len(facts))
        trace = client.get(f"/api/v1/tasks/{task_id}/trace", headers=headers["admin"]).raise_for_status().json()
        runs = trace.get("agent_runs") or []
        used = [run for run in runs if run.get("model_used")]
        report["agent_runs"] = [{key: run.get(key) for key in
                                ("agent_code", "status", "model", "model_used", "total_tokens", "latency_ms")}
                               for run in runs]
        report["total_tokens"] = sum(int(run.get("total_tokens") or 0) for run in runs)
        check("seven_business_agents_executed", len({run["agent_code"] for run in runs}) >= 7)
        check("real_model_output_used", bool(used) if model.get("enabled") else not used,
              model_output_count=len(used), model_configured=bool(model.get("enabled")))
        events = client.get(f"/api/v1/tasks/{task_id}/events", headers=headers["admin"]).raise_for_status().json()["items"]
        nodes = [event.get("node") for event in events]
        reflection = (current.get("analysis") or {}).get("reflection") or {}
        report["reflection"] = reflection
        report["reflection_revision_executed"] = "apply_ticket_revision" in nodes
        check("reflection_actually_evaluated", "evaluate_ticket_reflection" in nodes
              and bool(reflection.get("enabled")))
        if model.get("enabled"):
            check("review_model_output_used", any(run["agent_code"] == "review_agent" for run in used))
        recommendation = current.get("recommendation")
        if recommendation:
            check("refund_amount_matches_sandbox_payment", Decimal(str(recommendation["amount"])) == Decimal("268.00"))
            check("policy_evidence_present", bool(recommendation.get("policy_refs")))
            check("supervisor_gate_required", recommendation["risk_tier"] == "L3"
                  and recommendation["approval_requirement"] == "supervisor"
                  and recommendation["status"] == "pending_approval")
            path = f"/api/v1/recommendations/{recommendation['id']}/decision"
            decision = {"decision": "approve", "reason": "仅本地联调：已核对沙箱事实和示例政策，验证主管审批权限。"}
            forbidden = client.post(path, headers=headers["ops"], json=decision)
            check("operator_cannot_approve", forbidden.status_code == 403, http_status=forbidden.status_code)
            approved = client.post(path, headers=headers["reviewer"], json=decision)
            check("supervisor_can_approve", approved.status_code == 200
                  and approved.json().get("status") == "approved", http_status=approved.status_code)
        else:
            check("recommendation_present", False)

    # Explicitly sync synthetic metrics into this isolated DB; never normal data/pic.db.
    if not (state_dir.resolve() / "integration.db").is_file():
        raise ValueError("local integration DB not found")
    os.environ.update(integration_environment(state_dir, sandbox_port))
    import app.config as config_module

    # app.config exposes a convenience singleton at import time. This verifier
    # imported Settings through the launcher before applying the isolated env,
    # so both the lru_cache and compatibility singleton must be refreshed.
    config_module.get_settings.cache_clear()
    config_module.settings = config_module.get_settings()
    from app.config import get_settings
    from app.db.session import SessionLocal
    from app.models.identity import Tenant
    from app.services.metric_source_sync import sync_metric_source

    with SessionLocal() as session:
        tenant = session.query(Tenant).filter_by(code="demo").one()
        result = sync_metric_source(session, tenant_id=tenant.id, settings=get_settings())
        session.commit()
    report["metric_sync"] = result
    check("metric_source_synced_to_isolated_db", result["status"] == "synced" and result["rows_written"] > 0,
          rows_written=result["rows_written"])
    report["passed"] = all(item["passed"] for item in checks)
    report["passed_count"] = sum(item["passed"] for item in checks)
    report["check_count"] = len(checks)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8033)
    parser.add_argument("--sandbox-port", type=int, default=8091)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--allow-real-models", action="store_true")
    args = parser.parse_args()
    report = verify(args.port, args.state_dir, args.sandbox_port, args.allow_real_models)
    destination = args.state_dir / "workflow-verification.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Result: {report['passed_count']}/{report['check_count']}; report {destination}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

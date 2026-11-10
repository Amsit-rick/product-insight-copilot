from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "verify_scheduler_failover.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "verify_scheduler_failover_under_test",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_plan_only_does_not_require_or_call_database(tmp_path, monkeypatch):
    module = _load_module()
    output = tmp_path / "plan.json"
    monkeypatch.delenv(module.DATABASE_URL_ENV, raising=False)

    assert module.main(["--json-out", str(output)]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "scheduler-failover-rehearsal-v1"
    assert payload["executed"] is False
    assert payload["reason"] == "database_call_not_authorized"
    assert payload["production_ready"] is False


def test_authorized_rehearsal_rejects_non_postgres_database(tmp_path, monkeypatch):
    module = _load_module()
    output = tmp_path / "invalid.json"
    monkeypatch.setenv(module.DATABASE_URL_ENV, "sqlite:///should-not-count.db")

    assert (
        module.main(
            [
                "--allow-real-database-call",
                "--json-out",
                str(output),
            ]
        )
        == 1
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["executed"] is False
    assert payload["reason"] == "postgres_database_required"
    assert payload["local_postgres_failover_verified"] is False
    assert "should-not-count" not in json.dumps(payload)


def test_result_builder_never_claims_cross_host_or_production_readiness():
    module = _load_module()
    checks = {
        "holder_acquired": True,
        "concurrent_contender_blocked": True,
        "crashed_holder_did_not_release": True,
        "takeover_after_expiry": True,
        "takeover_released": True,
        "rehearsal_row_cleaned": True,
    }

    payload = module.build_result(
        executed=True,
        lease_seconds=3,
        checks=checks,
        takeover_wait_seconds=2.8,
    )

    assert payload["passed"] is True
    assert payload["local_postgres_failover_verified"] is True
    assert payload["cross_host_failover_verified"] is False
    assert payload["production_ready"] is False
    assert "owner_id" not in json.dumps(payload)
    assert "database_url" not in json.dumps(payload)

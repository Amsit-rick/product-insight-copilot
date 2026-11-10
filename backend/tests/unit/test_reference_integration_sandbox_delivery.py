"""统一集成 Sandbox 的活体与容器交付契约。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"


def test_reference_sandbox_verifier_uses_real_http_and_redacts_evidence(tmp_path):
    output = tmp_path / "reference-sandbox-result.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(BACKEND / "scripts" / "verify_reference_integration_sandbox.py"),
            "--json-out",
            str(output),
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == "reference-integration-sandbox-v1"
    assert report["status"] == "verified_local"
    assert report["transport"] == "real_http"
    assert report["self_hosted"] is True
    assert report["production_ready"] is False
    assert report["summary"]["failed"] == 0
    assert report["summary"]["passed"] >= 14

    serialized = json.dumps(report, ensure_ascii=False)
    combined_output = completed.stdout + completed.stderr + serialized
    for forbidden in (
        "local-sandbox-key",
        "local-webhook-secret",
        "test-sandbox-key",
        "test-webhook-secret",
        "order_amount",
        "receiver_phone",
        '"value"',
        '"alert"',
    ):
        assert forbidden not in combined_output


def test_reference_sandbox_is_an_explicit_dependency_free_compose_profile():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")

    assert "\n  integration-sandbox:\n" in compose
    section = compose.split("\n  integration-sandbox:\n", 1)[1].split(
        "\n\nnetworks:\n", 1
    )[0]
    assert 'profiles: ["sandbox"]' in section
    assert "depends_on:" not in section
    assert "postgres" not in section
    assert "redis" not in section
    assert "*pic-env" not in section
    assert "SANDBOX_API_KEY: ${SANDBOX_API_KEY:-local-sandbox-key}" in section
    assert (
        "SANDBOX_WEBHOOK_SECRET: "
        "${SANDBOX_WEBHOOK_SECRET:-local-webhook-secret}"
    ) in section
    assert '"${SANDBOX_PORT:-8091}:8091"' in section
    assert "reference_integration_api.py" in section
    assert "http://127.0.0.1:8091/health" in section

    assert "COPY backend/sandbox_services/ ./sandbox_services/" in dockerfile
    assert (
        "COPY backend/scripts/reference_integration_api.py "
        "./scripts/reference_integration_api.py"
    ) in dockerfile
    assert (
        "COPY backend/scripts/verify_reference_integration_sandbox.py "
        "./scripts/verify_reference_integration_sandbox.py"
    ) in dockerfile

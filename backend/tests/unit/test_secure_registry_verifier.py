from __future__ import annotations

import hashlib
import importlib.util
import json
import ssl
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "backend" / "scripts" / "verify_secure_registry.py"
SPEC = importlib.util.spec_from_file_location("secure_registry_verifier_under_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_probe_artifact_is_content_addressed_and_binds_source_sha():
    source_sha = "a" * 40
    artifact = MODULE.build_probe_artifact(source_sha)
    manifest = json.loads(artifact["manifest"])
    config = json.loads(artifact["config"])

    assert artifact["manifest_digest"] == "sha256:" + hashlib.sha256(
        artifact["manifest"]
    ).hexdigest()
    assert manifest["config"]["digest"] == artifact["config_digest"]
    assert manifest["layers"][0]["digest"] == artifact["layer_digest"]
    assert manifest["annotations"]["org.opencontainers.image.revision"] == source_sha
    assert config["config"]["Labels"]["org.opencontainers.image.revision"] == source_sha


@pytest.mark.parametrize(
    "location",
    [
        "http://localhost/v2/repo/blobs/uploads/id",
        "https://localhost/not-v2/id",
        "/outside/id",
    ],
)
def test_upload_location_rejects_insecure_or_out_of_scope_paths(location: str):
    with pytest.raises(ValueError, match="location"):
        MODULE._location_path(location)


def test_upload_location_preserves_registry_query():
    assert MODULE._location_path(
        "https://localhost/v2/repo/blobs/uploads/id?_state=abc"
    ) == "/v2/repo/blobs/uploads/id?_state=abc"


def test_readiness_waits_before_untrusted_ca_probe(monkeypatch, tmp_path: Path):
    ca = tmp_path / "ca.crt"
    ca.write_text("placeholder", encoding="utf-8")
    calls: list[tuple[Path | None, str | None]] = []
    monkeypatch.setattr(MODULE, "_upload_blob", lambda **kwargs: None)
    monkeypatch.setattr(MODULE, "x509_der_from_pem", lambda value: b"ca-der")

    artifact = MODULE.build_probe_artifact("b" * 40)

    def _manifest_request(**kwargs):  # noqa: ANN003
        calls.append((kwargs["ca_cert"], kwargs.get("username")))
        if kwargs["ca_cert"] is None:
            raise ssl.SSLCertVerificationError("untrusted")
        if kwargs.get("username") is None:
            return 401, {"www-authenticate": 'Basic realm="test"'}, b""
        if kwargs.get("password", "").endswith("-wrong"):
            return 401, {}, b""
        if kwargs["method"] == "PUT":
            return 201, {"docker-content-digest": artifact["manifest_digest"]}, b""
        if "/manifests/" in kwargs["path"]:
            return 200, {}, artifact["manifest"]
        return 200, {}, b""

    monkeypatch.setattr(MODULE, "_request", _manifest_request)
    payload = MODULE.verify_secure_registry(
        endpoint="https://127.0.0.1:15443",
        ca_cert=ca,
        source_sha="b" * 40,
        username="pic-ci",
        password="p" * 24,
    )

    assert calls[0] == (ca, "pic-ci")
    assert calls[1] == (None, None)
    assert payload["tls_verified"] is True
    assert payload["untrusted_tls_rejected"] is True
    assert payload["production_ready"] is False


def test_secure_registry_compose_and_rehearsal_are_fail_closed():
    compose = (ROOT / "deploy" / "docker-compose.secure-registry.yml").read_text(
        encoding="utf-8"
    )
    rehearsal = (ROOT / "scripts" / "rehearse-secure-registry.ps1").read_text(
        encoding="utf-8"
    )

    assert "registry:2@sha256:" in compose
    assert '127.0.0.1:${SECURE_REGISTRY_PORT:-15443}:5000' in compose
    assert "REGISTRY_HTTP_TLS_CERTIFICATE" in compose
    assert "REGISTRY_AUTH: htpasswd" in compose
    assert "read_only: true" in compose
    assert 'cap_drop: ["ALL"]' in compose
    assert 'security_opt: ["no-new-privileges:true"]' in compose
    assert "cpus:" in compose and "mem_limit:" in compose and "pids_limit:" in compose

    assert "RandomNumberGenerator]::Create()" in rehearsal
    assert "$Random.GetBytes($PasswordBytes)" in rehearsal
    assert "$Random.Dispose()" in rehearsal
    assert "Refusing secure Registry rehearsal from a dirty Git worktree" in rehearsal
    assert "$RegistryStarted = $true\n    docker compose" in rehearsal
    assert "finally {" in rehearsal
    assert "down -v --remove-orphans" in rehearsal
    assert "Remove-Item -LiteralPath $ResolvedConfig -Recurse -Force" in rehearsal
    assert "Write-Host $env:SECURE_REGISTRY_PASSWORD" not in rehearsal
    assert "--password $env:SECURE_REGISTRY_PASSWORD" not in rehearsal

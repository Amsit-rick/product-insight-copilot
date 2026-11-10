from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "backend" / "scripts" / "verify_self_hosted_runner.py"
SPEC = importlib.util.spec_from_file_location("self_hosted_runner_under_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SHA = "a" * 40


def _probe(**updates):
    payload = {
        "git_available": True,
        "git_version": "2.51.0",
        "bash_available": True,
        "bash_version": "5.2.37",
        "curl_available": True,
        "curl_version": "8.17.0",
        "python_available": True,
        "python_version": "3.12.12",
        "timeout_available": True,
        "timeout_version": "9.5",
        "git_head_available": True,
        "git_head": SHA,
        "git_status_available": True,
        "git_clean": True,
        "docker_cli_available": True,
        "docker_daemon_verified": True,
        "docker_buildx_available": True,
        "docker_compose_available": True,
        "docker_server_os": "linux",
        "docker_server_arch": "amd64",
        "docker_client_version": "29.7.2",
        "docker_server_version": "29.7.2",
        "docker_buildx_version": "0.36.1",
        "docker_compose_version": "5.4.0",
        "docker_endpoint_local": True,
        "runner_os": "linux",
        "runner_arch": "amd64",
        "runner_user_non_root": True,
        "memory_total_mb": 8192,
        "disk_free_mb": 51200,
    }
    payload.update(updates)
    return payload


def _evaluate(*, probe=None, group="enterprise-runner-group-42", scope="self_hosted_supply_chain_runner"):
    return MODULE.evaluate_runner_readiness(
        probe=probe or _probe(),
        provider="gitee_go" if scope == "self_hosted_supply_chain_runner" else "local_docker_desktop",
        scope=scope,
        source_sha=SHA,
        host_group_id=group,
        minimum_memory_mb=4096,
        minimum_disk_mb=10240,
    )


def test_self_hosted_runner_passes_without_exposing_host_group_id():
    evidence = _evaluate()

    assert evidence["passed"] is True
    assert evidence["supply_chain_runner_ready"] is True
    assert evidence["host_group_configured"] is True
    assert len(evidence["host_group_fingerprint"]) == 16
    assert evidence["docker_daemon_verified"] is True
    assert evidence["docker_buildx_available"] is True
    assert evidence["docker_compose_available"] is True
    assert evidence["checks"]["git_cli_available"] is True
    assert evidence["checks"]["bash_available"] is True
    assert evidence["checks"]["curl_available"] is True
    assert evidence["checks"]["python_available"] is True
    assert evidence["checks"]["timeout_available"] is True
    assert evidence["production_ready"] is False
    assert "enterprise-runner-group-42" not in json.dumps(evidence)


def test_self_hosted_runner_rejects_placeholder_root_and_remote_daemon():
    placeholder = _evaluate(group="replace-with-gitee-host-group-id")
    root_runner = _evaluate(probe=_probe(runner_user_non_root=False))
    remote_daemon = _evaluate(probe=_probe(docker_endpoint_local=False))

    assert placeholder["passed"] is False
    assert placeholder["checks"]["host_group_configured"] is False
    assert root_runner["passed"] is False
    assert root_runner["checks"]["runner_user_non_root"] is False
    assert remote_daemon["passed"] is False
    assert remote_daemon["checks"]["docker_endpoint_local"] is False


def test_local_rehearsal_allows_windows_host_but_never_claims_production():
    evidence = _evaluate(
        scope="local_host_rehearsal",
        group="",
        probe=_probe(runner_os="windows", runner_user_non_root=False),
    )

    assert evidence["passed"] is True
    assert evidence["scope"] == "local_host_rehearsal"
    assert evidence["host_group_configured"] is False
    assert evidence["production_ready"] is False

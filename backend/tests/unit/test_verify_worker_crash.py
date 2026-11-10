from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "verify_worker_crash.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_worker_crash_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docker_worker_listing_is_scoped_to_compose_project(monkeypatch):
    module = _load_module()
    module.COMPOSE_PROJECT = "pic-ticket-audit"
    seen: list[list[str]] = []

    def fake_run(args, *, encoding="utf-8"):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="abc123\ndef456\n", stderr="")

    monkeypatch.setattr(module, "_run_command", fake_run)

    assert module._docker_workers() == ["abc123", "def456"]
    assert "label=com.docker.compose.project=pic-ticket-audit" in seen[0]
    assert "label=com.docker.compose.service=worker" in seen[0]


def test_docker_kill_removes_exact_claim_holder_container(monkeypatch):
    module = _load_module()
    module.KILL_MODE = "docker"
    monkeypatch.setattr(module, "_docker_workers", lambda: ["abc123def456", "fff999eee888"])
    seen: list[str] = []

    def fake_run(args, *, encoding="utf-8"):
        seen.extend(args)
        return subprocess.CompletedProcess(args, 0, stdout="abc123def456\n", stderr="")

    monkeypatch.setattr(module, "_run_command", fake_run)

    ok, detail = module._kill_worker("abc123def456-1")

    assert ok is True
    assert seen == ["docker", "rm", "-f", "abc123def456"]
    assert detail == "abc123def456"


def test_docker_kill_refuses_ambiguous_or_missing_holder(monkeypatch):
    module = _load_module()
    module.KILL_MODE = "docker"
    monkeypatch.setattr(module, "_docker_workers", lambda: ["other-container"])

    ok, detail = module._kill_worker("abc123def456-1")

    assert ok is False
    assert "无法唯一定位" in detail

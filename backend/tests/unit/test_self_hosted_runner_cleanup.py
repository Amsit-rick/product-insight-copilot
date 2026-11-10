from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "backend" / "scripts" / "cleanup_self_hosted_runner.py"
SPEC = importlib.util.spec_from_file_location("runner_cleanup_under_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _stale_lock(path: Path, *, pid: int = 999999, started_at: int = 1000) -> Path:
    path.mkdir(parents=True)
    (path / "owner.json").write_text(
        json.dumps({"pid": pid, "started_at_epoch": started_at}), encoding="utf-8"
    )
    return path


def test_cleanup_rejects_paths_outside_repository_tmp(tmp_path: Path):
    with pytest.raises(ValueError, match="repository .tmp"):
        MODULE.inspect_recovery(
            project="pic-safe-project",
            lock_dir=tmp_path / "runner.lock",
            minimum_age_seconds=300,
            now=2000,
        )


def test_cleanup_rejects_fresh_or_live_lock(monkeypatch, tmp_path: Path):
    lock = ROOT / ".tmp" / f"pytest-cleanup-{tmp_path.name}.lock"
    _stale_lock(lock, pid=42, started_at=1900)
    try:
        with pytest.raises(ValueError, match="not old enough"):
            MODULE.inspect_recovery(
                project="pic-safe-project",
                lock_dir=lock,
                minimum_age_seconds=300,
                now=2000,
            )
        monkeypatch.setattr(MODULE, "_pid_running", lambda pid: True)
        with pytest.raises(ValueError, match="still running"):
            MODULE.inspect_recovery(
                project="pic-safe-project",
                lock_dir=lock,
                minimum_age_seconds=300,
                now=3000,
            )
    finally:
        (lock / "owner.json").unlink(missing_ok=True)
        lock.rmdir()


def test_cleanup_dry_run_is_non_destructive(monkeypatch, tmp_path: Path):
    lock = ROOT / ".tmp" / f"pytest-cleanup-{tmp_path.name}.lock"
    _stale_lock(lock)
    monkeypatch.setattr(MODULE, "_pid_running", lambda pid: False)
    try:
        payload = MODULE.recover_runner(
            project="pic-safe-project",
            lock_dir=lock,
            minimum_age_seconds=300,
            apply=False,
        )
        assert payload["mode"] == "dry_run"
        assert payload["compose_cleanup_executed"] is False
        assert payload["global_docker_cleanup_used"] is False
        assert lock.is_dir()
    finally:
        (lock / "owner.json").unlink(missing_ok=True)
        lock.rmdir()


def test_cleanup_apply_targets_only_named_project_and_removes_lock(monkeypatch, tmp_path: Path):
    lock = ROOT / ".tmp" / f"pytest-cleanup-apply-{tmp_path.name}.lock"
    _stale_lock(lock)
    monkeypatch.setattr(MODULE, "_pid_running", lambda pid: False)
    captured = {}

    class _Completed:
        returncode = 0

    def _run(command, **kwargs):  # noqa: ANN001, ANN003
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(MODULE.subprocess, "run", _run)
    payload = MODULE.recover_runner(
        project="pic-safe-project",
        lock_dir=lock,
        minimum_age_seconds=300,
        apply=True,
    )

    assert captured["command"] == [
        "docker",
        "compose",
        "-p",
        "pic-safe-project",
        "-f",
        str(MODULE.COMPOSE_FILE),
        "down",
        "-v",
        "--remove-orphans",
    ]
    assert payload["compose_cleanup_executed"] is True
    assert payload["global_docker_cleanup_used"] is False
    assert not lock.exists()

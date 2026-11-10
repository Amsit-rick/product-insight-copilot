from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "verify_dependency_locks.py"
spec = importlib.util.spec_from_file_location("verify_dependency_locks_under_test", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _write_lock(path: Path, packages: dict[str, str]) -> None:
    rows = []
    for index, (name, version) in enumerate(packages.items(), start=1):
        rows.append(f"{name}=={version} \\")
        rows.append(f"    --hash=sha256:{index:064x}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _valid_backend(tmp_path: Path) -> Path:
    (tmp_path / "requirements-runtime.in").write_text("fastapi>=0.1\n", encoding="utf-8")
    (tmp_path / "requirements-dev.in").write_text(
        (
            "-c requirements-runtime.lock\n"
            "-r requirements-runtime.in\n"
            "coverage>=7.17,<8\n"
            "pytest>=8\n"
            "pytest-asyncio>=0.24\n"
        ),
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text(
        "# compatibility\n-r requirements-dev-windows.lock\n", encoding="utf-8"
    )
    _write_lock(tmp_path / "requirements-runtime.lock", {"fastapi": "1.0.0"})
    _write_lock(
        tmp_path / "requirements-dev.lock",
        {
            "coverage": "7.17.2",
            "fastapi": "1.0.0",
            "pytest": "8.4.0",
            "pytest-asyncio": "0.25.0",
        },
    )
    _write_lock(
        tmp_path / "requirements-dev-windows.lock",
        {
            "fastapi": "1.0.0",
            "colorama": "0.4.6",
            "coverage": "7.17.2",
            "pytest": "8.4.0",
            "pytest-asyncio": "0.25.0",
        },
    )
    return tmp_path


def test_repository_dependency_locks_pass_contract():
    payload = module.verify_dependency_locks(BACKEND)

    assert payload["status"] == "passed"
    assert payload["runtime_package_count"] < payload["development_package_count"]
    assert payload["production_ready"] is False


def test_lock_contract_rejects_missing_hash(tmp_path: Path):
    backend = _valid_backend(tmp_path)
    (backend / "requirements-runtime.lock").write_text("fastapi==1.0.0\n", encoding="utf-8")

    with pytest.raises(module.LockVerificationError, match="缺少 SHA-256"):
        module.verify_dependency_locks(backend)


def test_lock_contract_rejects_test_package_in_runtime(tmp_path: Path):
    backend = _valid_backend(tmp_path)
    _write_lock(
        backend / "requirements-runtime.lock",
        {"fastapi": "1.0.0", "pytest": "8.4.0"},
    )

    with pytest.raises(module.LockVerificationError, match="运行时锁包含测试依赖"):
        module.verify_dependency_locks(backend)


def test_lock_contract_rejects_missing_coverage_tool(tmp_path: Path):
    backend = _valid_backend(tmp_path)
    _write_lock(
        backend / "requirements-dev.lock",
        {"fastapi": "1.0.0", "pytest": "8.4.0", "pytest-asyncio": "0.25.0"},
    )

    with pytest.raises(module.LockVerificationError, match="coverage"):
        module.verify_dependency_locks(backend)


def test_lock_contract_rejects_index_or_credentials(tmp_path: Path):
    backend = _valid_backend(tmp_path)
    path = backend / "requirements-dev.lock"
    path.write_text("--index-url https://user:secret@example.test/simple\n" + path.read_text(), encoding="utf-8")

    with pytest.raises(module.LockVerificationError, match="索引地址"):
        module.verify_dependency_locks(backend)

"""Fail closed when generated Python dependency locks lose reproducibility."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


BACKEND = Path(__file__).resolve().parents[1]
HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)")
REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)")
FORBIDDEN_RE = re.compile(
    r"(?:--index-url|--extra-index-url|--trusted-host|https?://[^\s/]+:[^\s/@]+@)",
    re.IGNORECASE,
)


class LockVerificationError(RuntimeError):
    """Raised when a lock contract is incomplete or unsafe."""


def _logical_requirements(path: Path) -> list[str]:
    logical: list[str] = []
    current: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current.append(stripped.removesuffix("\\").strip())
        if not stripped.endswith("\\"):
            logical.append(" ".join(current))
            current = []
    if current:
        raise LockVerificationError(f"{path.name}: 文件末尾存在未完成的续行")
    return logical


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _parse_lock(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise LockVerificationError(f"缺少锁文件: {path.name}")
    text = path.read_text(encoding="utf-8")
    if FORBIDDEN_RE.search(text):
        raise LockVerificationError(f"{path.name}: 包含索引地址、trusted host 或 URL 凭据")

    packages: dict[str, dict[str, Any]] = {}
    for logical in _logical_requirements(path):
        match = REQUIREMENT_RE.match(logical)
        if not match:
            raise LockVerificationError(f"{path.name}: 非精确版本依赖: {logical[:120]}")
        name = _normalize_name(match.group(1))
        hashes = sorted(set(HASH_RE.findall(logical)))
        if not hashes:
            raise LockVerificationError(f"{path.name}: {name} 缺少 SHA-256")
        if name in packages:
            raise LockVerificationError(f"{path.name}: 重复依赖 {name}")
        packages[name] = {"version": match.group(2), "hash_count": len(hashes)}
    if not packages:
        raise LockVerificationError(f"{path.name}: 未解析到依赖")
    return packages


def verify_dependency_locks(backend: Path = BACKEND) -> dict[str, Any]:
    runtime_input = backend / "requirements-runtime.in"
    dev_input = backend / "requirements-dev.in"
    compatibility = backend / "requirements.txt"
    for path in (runtime_input, dev_input, compatibility):
        if not path.is_file():
            raise LockVerificationError(f"缺少依赖入口: {path.name}")

    dev_input_lines = {
        line.strip()
        for line in dev_input.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required_dev_layers = {"-c requirements-runtime.lock", "-r requirements-runtime.in"}
    if not required_dev_layers.issubset(dev_input_lines):
        raise LockVerificationError("requirements-dev.in 未受运行时锁约束")

    runtime = _parse_lock(backend / "requirements-runtime.lock")
    dev = _parse_lock(backend / "requirements-dev.lock")
    windows_dev = _parse_lock(backend / "requirements-dev-windows.lock")
    test_packages = {"coverage", "pytest", "pytest-asyncio"}
    unexpected_runtime = sorted(test_packages.intersection(runtime))
    if unexpected_runtime:
        raise LockVerificationError(
            "运行时锁包含测试依赖: " + ", ".join(unexpected_runtime)
        )
    missing_dev = sorted(test_packages.difference(dev))
    if missing_dev:
        raise LockVerificationError("开发锁缺少测试依赖: " + ", ".join(missing_dev))
    missing_windows_dev = sorted(test_packages.difference(windows_dev))
    if missing_windows_dev:
        raise LockVerificationError(
            "Windows 开发锁缺少测试依赖: " + ", ".join(missing_windows_dev)
        )

    drift = sorted(
        name
        for name, value in runtime.items()
        if name not in dev or dev[name]["version"] != value["version"]
    )
    if drift:
        raise LockVerificationError("运行时与开发锁版本漂移: " + ", ".join(drift))

    windows_only_exclusions = {"uvloop"}
    windows_drift = sorted(
        name
        for name, value in runtime.items()
        if name not in windows_only_exclusions
        and (name not in windows_dev or windows_dev[name]["version"] != value["version"])
    )
    if windows_drift:
        raise LockVerificationError(
            "运行时与 Windows 开发锁版本漂移: " + ", ".join(windows_drift)
        )
    if "uvloop" in windows_dev or "colorama" not in windows_dev:
        raise LockVerificationError("Windows 开发锁的平台依赖不正确")

    compatibility_lines = [
        line.strip()
        for line in compatibility.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if compatibility_lines != ["-r requirements-dev-windows.lock"]:
        raise LockVerificationError(
            "requirements.txt 必须只兼容引用 requirements-dev-windows.lock"
        )

    return {
        "status": "passed",
        "runtime_package_count": len(runtime),
        "development_package_count": len(dev),
        "windows_development_package_count": len(windows_dev),
        "runtime_test_dependencies": unexpected_runtime,
        "development_test_dependencies_present": sorted(test_packages),
        "windows_platform_contract": {"excluded": ["uvloop"], "required": ["colorama"]},
        "hashes_required": True,
        "production_ready": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, default=BACKEND)
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = verify_dependency_locks(args.backend.resolve())
    except LockVerificationError as exc:
        print(f"[FAIL] {exc}")
        return 1
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

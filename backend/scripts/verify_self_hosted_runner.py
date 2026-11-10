"""Verify that a runner can execute the image supply-chain workflow.

The evidence is deliberately sanitized: it never records host names, user names,
filesystem paths, Docker endpoints, credentials, or the raw Gitee host-group ID.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PLACEHOLDER_HOST_GROUPS = {
    "",
    "replace-with-gitee-host-group-id",
    "changeme",
    "placeholder",
}
SUPPORTED_PROVIDERS = {"gitee_go", "local_docker_desktop"}
SUPPORTED_SCOPES = {"self_hosted_supply_chain_runner", "local_host_rehearsal"}


def _run(command: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return completed.returncode == 0, completed.stdout.strip()


def _total_memory_mb() -> int:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_physical // (1024 * 1024))
        return 0
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return 0
    return int(pages * page_size // (1024 * 1024))


def _normalize_arch(value: str) -> str:
    lowered = value.strip().lower()
    return {"x86_64": "amd64", "x64": "amd64", "aarch64": "arm64"}.get(
        lowered, lowered
    )


def _version(value: str) -> str | None:
    match = re.search(r"\bv?(\d+\.\d+(?:\.\d+)?)\b", value)
    return match.group(1) if match else None


def collect_probe() -> dict[str, Any]:
    git_available = shutil.which("git") is not None
    bash_available = shutil.which("bash") is not None
    curl_available = shutil.which("curl") is not None
    timeout_available = shutil.which("timeout") is not None
    docker_available = shutil.which("docker") is not None
    git_head_ok, git_head = _run(["git", "rev-parse", "HEAD"]) if git_available else (False, "")
    git_status_ok, git_status = (
        _run(["git", "status", "--porcelain"]) if git_available else (False, "")
    )
    git_version_ok, git_version_raw = (
        _run(["git", "--version"]) if git_available else (False, "")
    )
    bash_version_ok, bash_version_raw = (
        _run(["bash", "--version"]) if bash_available else (False, "")
    )
    curl_version_ok, curl_version_raw = (
        _run(["curl", "--version"]) if curl_available else (False, "")
    )
    timeout_version_ok, timeout_version_raw = (
        _run(["timeout", "--help"]) if timeout_available else (False, "")
    )

    docker_daemon = False
    docker_server_os = ""
    docker_server_arch = ""
    docker_client_version = None
    docker_server_version = None
    if docker_available:
        version_ok, version_raw = _run(["docker", "version", "--format", "{{json .}}"])
        if version_ok:
            try:
                version_payload = json.loads(version_raw)
            except json.JSONDecodeError:
                version_payload = {}
            client = version_payload.get("Client") or {}
            server = version_payload.get("Server") or {}
            docker_daemon = isinstance(server, dict) and bool(server.get("Version"))
            docker_client_version = _version(str(client.get("Version") or ""))
            docker_server_version = _version(str(server.get("Version") or ""))
            docker_server_os = str(server.get("Os") or "").lower()
            docker_server_arch = _normalize_arch(str(server.get("Arch") or ""))

    buildx_ok, buildx_raw = (
        _run(["docker", "buildx", "version"]) if docker_available else (False, "")
    )
    compose_ok, compose_raw = (
        _run(["docker", "compose", "version"]) if docker_available else (False, "")
    )
    docker_host = os.environ.get("DOCKER_HOST", "").strip().lower()
    docker_endpoint_local = not docker_host or docker_host.startswith(("unix://", "npipe://"))
    runner_os = platform.system().lower()
    runner_arch = _normalize_arch(platform.machine())
    is_root = bool(hasattr(os, "geteuid") and os.geteuid() == 0)

    return {
        "git_available": git_available,
        "git_version": _version(git_version_raw) if git_version_ok else None,
        "bash_available": bash_available,
        "bash_version": _version(bash_version_raw) if bash_version_ok else None,
        "curl_available": curl_available,
        "curl_version": _version(curl_version_raw) if curl_version_ok else None,
        "python_available": True,
        "python_version": platform.python_version(),
        "timeout_available": timeout_available,
        "timeout_version": _version(timeout_version_raw) if timeout_version_ok else None,
        "git_head_available": git_head_ok,
        "git_head": git_head,
        "git_status_available": git_status_ok,
        "git_clean": git_status_ok and not git_status,
        "docker_cli_available": docker_available,
        "docker_daemon_verified": docker_daemon,
        "docker_buildx_available": buildx_ok,
        "docker_compose_available": compose_ok,
        "docker_server_os": docker_server_os,
        "docker_server_arch": docker_server_arch,
        "docker_client_version": docker_client_version,
        "docker_server_version": docker_server_version,
        "docker_buildx_version": _version(buildx_raw),
        "docker_compose_version": _version(compose_raw),
        "docker_endpoint_local": docker_endpoint_local,
        "runner_os": runner_os,
        "runner_arch": runner_arch,
        "runner_user_non_root": not is_root,
        "memory_total_mb": _total_memory_mb(),
        "disk_free_mb": int(shutil.disk_usage(ROOT).free // (1024 * 1024)),
    }


def evaluate_runner_readiness(
    *,
    probe: dict[str, Any],
    provider: str,
    scope: str,
    source_sha: str,
    host_group_id: str,
    minimum_memory_mb: int,
    minimum_disk_mb: int,
) -> dict[str, Any]:
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("unsupported runner provider")
    if scope not in SUPPORTED_SCOPES:
        raise ValueError("unsupported runner scope")
    if scope == "self_hosted_supply_chain_runner" and provider != "gitee_go":
        raise ValueError("self-hosted runner scope requires gitee_go provider")
    if minimum_memory_mb < 256 or minimum_disk_mb < 1024:
        raise ValueError("runner resource thresholds are below the safe minimum")

    normalized_group = host_group_id.strip()
    host_group_configured = normalized_group.lower() not in PLACEHOLDER_HOST_GROUPS
    source_sha_valid = bool(SHA_RE.fullmatch(source_sha))
    checks = {
        "git_cli_available": probe.get("git_available") is True,
        "bash_available": probe.get("bash_available") is True,
        "curl_available": probe.get("curl_available") is True,
        "python_available": probe.get("python_available") is True,
        "timeout_available": probe.get("timeout_available") is True,
        "source_sha_valid": source_sha_valid,
        "source_checkout_matches": source_sha_valid
        and probe.get("git_head") == source_sha,
        "source_checkout_clean": probe.get("git_clean") is True,
        "docker_cli_available": probe.get("docker_cli_available") is True,
        "docker_daemon_verified": probe.get("docker_daemon_verified") is True,
        "docker_buildx_available": probe.get("docker_buildx_available") is True,
        "docker_compose_available": probe.get("docker_compose_available") is True,
        "docker_server_linux": probe.get("docker_server_os") == "linux",
        "docker_arch_supported": probe.get("docker_server_arch") == "amd64",
        "docker_endpoint_local": probe.get("docker_endpoint_local") is True,
        "memory_capacity_ready": int(probe.get("memory_total_mb") or 0)
        >= minimum_memory_mb,
        "disk_capacity_ready": int(probe.get("disk_free_mb") or 0) >= minimum_disk_mb,
        "host_group_configured": host_group_configured,
        "runner_os_linux": probe.get("runner_os") == "linux",
        "runner_user_non_root": probe.get("runner_user_non_root") is True,
    }
    required = [
        "git_cli_available",
        "bash_available",
        "curl_available",
        "python_available",
        "timeout_available",
        "source_sha_valid",
        "source_checkout_matches",
        "source_checkout_clean",
        "docker_cli_available",
        "docker_daemon_verified",
        "docker_buildx_available",
        "docker_compose_available",
        "docker_server_linux",
        "docker_arch_supported",
        "docker_endpoint_local",
        "memory_capacity_ready",
        "disk_capacity_ready",
    ]
    if scope == "self_hosted_supply_chain_runner":
        required.extend(
            ["host_group_configured", "runner_os_linux", "runner_user_non_root"]
        )
    ready = all(checks[name] for name in required)
    group_fingerprint = (
        hashlib.sha256(normalized_group.encode("utf-8")).hexdigest()[:16]
        if host_group_configured
        else None
    )
    return {
        "schema_version": "self-hosted-runner-readiness-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "executed": True,
        "passed": ready,
        "provider": provider,
        "scope": scope,
        "source_git_sha": source_sha,
        "host_group_configured": host_group_configured,
        "host_group_fingerprint": group_fingerprint,
        "runner_os": probe.get("runner_os"),
        "runner_arch": probe.get("runner_arch"),
        "runner_user_non_root": probe.get("runner_user_non_root") is True,
        "git_version": probe.get("git_version"),
        "bash_version": probe.get("bash_version"),
        "curl_version": probe.get("curl_version"),
        "python_version": probe.get("python_version"),
        "timeout_version": probe.get("timeout_version"),
        "docker_server_os": probe.get("docker_server_os"),
        "docker_server_arch": probe.get("docker_server_arch"),
        "docker_cli_available": probe.get("docker_cli_available") is True,
        "docker_daemon_verified": probe.get("docker_daemon_verified") is True,
        "docker_buildx_available": probe.get("docker_buildx_available") is True,
        "docker_compose_available": probe.get("docker_compose_available") is True,
        "docker_endpoint_local": probe.get("docker_endpoint_local") is True,
        "docker_client_version": probe.get("docker_client_version"),
        "docker_server_version": probe.get("docker_server_version"),
        "docker_buildx_version": probe.get("docker_buildx_version"),
        "docker_compose_version": probe.get("docker_compose_version"),
        "memory_total_mb": int(probe.get("memory_total_mb") or 0),
        "disk_free_mb": int(probe.get("disk_free_mb") or 0),
        "minimum_memory_mb": minimum_memory_mb,
        "minimum_disk_mb": minimum_disk_mb,
        "checks": checks,
        "supply_chain_runner_ready": ready,
        "image_supply_chain_executed": False,
        "enterprise_registry_verified": False,
        "cross_host_verified": False,
        "production_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=sorted(SUPPORTED_PROVIDERS), required=True)
    parser.add_argument("--scope", choices=sorted(SUPPORTED_SCOPES), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--host-group-id", default="")
    parser.add_argument("--minimum-memory-mb", type=int, default=4096)
    parser.add_argument("--minimum-disk-mb", type=int, default=10240)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = evaluate_runner_readiness(
        probe=collect_probe(),
        provider=args.provider,
        scope=args.scope,
        source_sha=args.source_sha,
        host_group_id=args.host_group_id,
        minimum_memory_mb=args.minimum_memory_mb,
        minimum_disk_mb=args.minimum_disk_mb,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Render and verify the source-free immutable release Compose model."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
APP_SERVICES = (
    "migrate",
    "api",
    "worker",
    "proactive-scheduler",
    "integration-sandbox",
)


def verify_release_compose(
    image: str,
    *,
    pull_policy: str = "always",
    root: Path = ROOT,
) -> dict[str, Any]:
    if not IMAGE_RE.fullmatch(image):
        raise ValueError("PIC_API_IMAGE 必须是 name@sha256:<64位十六进制> 格式")
    env = os.environ.copy()
    env["PIC_API_IMAGE"] = image
    env["PIC_RELEASE_PULL_POLICY"] = pull_policy
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(root / "deploy" / "docker-compose.yml"),
            "-f",
            str(root / "deploy" / "docker-compose.release.yml"),
            "--profile",
            "proactive",
            "--profile",
            "sandbox",
            "config",
            "--format",
            "json",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip()[:600])
    model = json.loads(completed.stdout)
    services = model.get("services") or {}
    failures: list[str] = []
    summary: dict[str, dict[str, Any]] = {}
    for service_name in APP_SERVICES:
        service = services.get(service_name)
        if not isinstance(service, dict):
            failures.append(f"缺少应用服务 {service_name}")
            continue
        service_summary = {
            "image": service.get("image"),
            "has_build": "build" in service,
            "pull_policy": service.get("pull_policy"),
        }
        summary[service_name] = service_summary
        if service_summary["image"] != image:
            failures.append(f"{service_name} 未使用指定 digest 镜像")
        if service_summary["has_build"]:
            failures.append(f"{service_name} 仍包含 build")
        if service_summary["pull_policy"] != pull_policy:
            failures.append(f"{service_name} pull_policy 不匹配")
    if failures:
        raise RuntimeError("；".join(failures))
    return {
        "schema_version": "release-compose-probe-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "executed": True,
        "passed": True,
        "status": "passed",
        "image_reference": image,
        "source_build_disabled": True,
        "pull_policy": pull_policy,
        "services": summary,
        "production_ready": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--pull-policy", choices=("always", "never", "missing"), default="always")
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = verify_release_compose(args.image, pull_policy=args.pull_policy)
    except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
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

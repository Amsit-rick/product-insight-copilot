"""Build an atomic manifest for the read-only delivery evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.services.delivery_evidence_contract import (  # noqa: E402
    REMOTE_CI_ATTACHMENT_CONTRACTS,
    remote_ci_attachment_is_safe,
)

DEFAULT_EVIDENCE_DIR = ROOT / "docs" / "testing" / "artifacts"
REPORT_FILE = "after-sales-release-readiness.json"
CHECK_FILE = "after-sales-delivery-check.json"
MANIFEST_FILE = "delivery-evidence-manifest.json"
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024


def _read_payload(path: Path, expected_schema: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"缺少证据文件：{path.name}")
    if path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError(f"证据文件过大：{path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"证据文件无效：{path.name}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != expected_schema:
        raise ValueError(f"证据 schema 不匹配：{path.name}")
    return payload


def _timestamp(value: Any, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 不是有效时间戳") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_delivery_evidence_manifest(
    evidence_dir: str | Path,
    *,
    generated_at: datetime | None = None,
    attachment_names: Iterable[str] = (),
) -> dict[str, Any]:
    root = Path(evidence_dir).resolve()
    report_path = root / REPORT_FILE
    check_path = root / CHECK_FILE
    report = _read_payload(report_path, "after-sales-release-readiness-v5")
    check = _read_payload(check_path, "after-sales-delivery-check-v5")
    report_time = _timestamp(report.get("generated_at"), field=f"{REPORT_FILE}.generated_at")
    check_time = _timestamp(check.get("generated_at"), field=f"{CHECK_FILE}.generated_at")
    source_report_time = _timestamp(
        check.get("source_report_generated_at"),
        field=f"{CHECK_FILE}.source_report_generated_at",
    )
    if source_report_time != report_time:
        raise ValueError("交付检查不属于当前预发布报告批次")
    if check_time < report_time:
        raise ValueError("交付检查生成时间早于预发布报告")

    release_sha = str(
        (report.get("release_evidence") or {}).get("commit_sha")
        or ((report.get("local_evidence") or {}).get("source_control") or {}).get("commit_sha")
        or ""
    ).strip()
    if not release_sha:
        raise ValueError("预发布报告缺少发布提交 SHA")

    created = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if created < check_time:
        raise ValueError("manifest 生成时间早于交付检查")
    files = {
        REPORT_FILE: {
            "sha256": _sha256(report_path),
            "generated_at": report_time.isoformat(),
        },
        CHECK_FILE: {
            "sha256": _sha256(check_path),
            "generated_at": check_time.isoformat(),
        },
    }
    seen_attachments: set[str] = set()
    for raw_name in attachment_names:
        name = str(raw_name or "").strip()
        contract = REMOTE_CI_ATTACHMENT_CONTRACTS.get(name)
        if contract is None:
            raise ValueError(f"不支持的交付证据附件：{name or '<empty>'}")
        if name in seen_attachments:
            raise ValueError(f"重复的交付证据附件：{name}")
        seen_attachments.add(name)
        path = root / name
        payload = _read_payload(path, contract["schema_version"])
        if not remote_ci_attachment_is_safe(name, payload):
            raise ValueError(f"远程 CI 附件无效或不得声明生产就绪：{name}")
        attachment_time = _timestamp(
            payload.get("generated_at"),
            field=f"{name}.generated_at",
        )
        if created < attachment_time:
            raise ValueError(f"manifest 生成时间早于附件：{name}")
        files[name] = {
            "sha256": _sha256(path),
            "generated_at": attachment_time.isoformat(),
            "schema_version": contract["schema_version"],
            "role": contract["role"],
        }
    bundle_material = json.dumps(
        {"release_sha": release_sha, "files": files},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": "delivery-evidence-manifest-v1",
        "generated_at": created.isoformat(),
        "release_sha": release_sha,
        "bundle_id": hashlib.sha256(bundle_material).hexdigest(),
        "files": files,
    }


def write_manifest_atomic(manifest: dict[str, Any], output: str | Path) -> Path:
    target = Path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    try:
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成绑定同批次报告与检查的只读交付证据 manifest")
    parser.add_argument("--evidence-dir", default=str(DEFAULT_EVIDENCE_DIR))
    parser.add_argument("--output")
    parser.add_argument(
        "--attachment",
        action="append",
        default=[],
        choices=sorted(REMOTE_CI_ATTACHMENT_CONTRACTS),
        help="纳入 manifest 哈希校验的远程 CI 脱敏附件，可重复",
    )
    args = parser.parse_args(argv)
    evidence_dir = Path(args.evidence_dir).resolve()
    output = Path(args.output).resolve() if args.output else evidence_dir / MANIFEST_FILE
    try:
        manifest = build_delivery_evidence_manifest(
            evidence_dir,
            attachment_names=args.attachment,
        )
        target = write_manifest_atomic(manifest, output)
    except ValueError as exc:
        print(f"交付证据打包失败：{exc}")
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"交付证据 manifest={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

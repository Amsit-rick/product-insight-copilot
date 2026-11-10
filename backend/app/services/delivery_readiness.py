"""把正式发布证据转换成管理员可读的只读快照。

本模块不会重新运行门禁、生成证据或批准发布。它只读取固定文件，并在证据
缺失、过期、版本未绑定或结构不完整时失败关闭。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services.delivery_evidence_contract import (
    REMOTE_CI_ATTACHMENT_CONTRACTS,
    remote_ci_attachment_is_safe,
)


REPORT_FILE = "after-sales-release-readiness.json"
CHECK_FILE = "after-sales-delivery-check.json"
MANIFEST_FILE = "delivery-evidence-manifest.json"
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024

EXPECTED_GATES = (
    ("fact_platform_sandbox", "订单、支付、物流与售后事实接口"),
    ("metric_source", "真实业务指标源"),
    ("message_channel", "站外消息通知渠道"),
    ("production_infrastructure", "生产部署与故障切换"),
    ("real_model_holdout", "真实模型独立评测"),
    ("usability_test", "客服与主管真人可用性"),
)


def _read_json(path: Path, *, missing_reason: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if not path.is_file():
            return None, missing_reason
        if path.stat().st_size > MAX_EVIDENCE_BYTES:
            return None, f"{path.stem}_too_large"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None, f"{path.stem}_invalid"
        return payload, None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, f"{path.stem}_invalid"


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_EVIDENCE_BYTES:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _short_text(value: Any, limit: int = 1000) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _gate_rows(report: dict[str, Any]) -> tuple[list[dict[str, str]], bool]:
    source = report.get("external_gates")
    rows_by_key = {
        str(item.get("key")): item
        for item in source
        if isinstance(item, dict) and item.get("key")
    } if isinstance(source, list) else {}
    rows: list[dict[str, str]] = []
    complete = True
    for key, label in EXPECTED_GATES:
        item = rows_by_key.get(key)
        if not item:
            rows.append({"key": key, "label": label, "status": "not_reported", "required": ""})
            complete = False
            continue
        status = _short_text(item.get("status"), 80) or "not_reported"
        if status not in {"verified_external", "blocked_external"}:
            status = "not_reported"
            complete = False
        rows.append(
            {
                "key": key,
                "label": label,
                "status": status,
                "required": _short_text(item.get("required")),
            }
        )
    return rows, complete


def build_delivery_readiness_snapshot(
    evidence_dir: str | Path,
    *,
    expected_release_sha: str = "",
    max_age_hours: int = 24,
    now: datetime | None = None,
) -> dict[str, Any]:
    """读取正式证据并返回脱敏快照；任何不确定性都不会放行生产状态。"""
    root = Path(evidence_dir)
    manifest, manifest_error = _read_json(
        root / MANIFEST_FILE,
        missing_reason="evidence_manifest_missing",
    )
    report, report_error = _read_json(root / REPORT_FILE, missing_reason="release_report_missing")
    check, check_error = _read_json(root / CHECK_FILE, missing_reason="delivery_check_missing")
    reasons = [reason for reason in (manifest_error, report_error, check_error) if reason]

    base: dict[str, Any] = {
        "schema_version": "delivery-readiness-snapshot-v1",
        "status": "missing" if manifest is None and report is None and check is None else "invalid",
        "evidence_valid": False,
        "evidence_bundle_verified": False,
        "evidence_fresh": False,
        "release_sha_verified": None,
        "trusted_production_ready": False,
        "reasons": reasons,
        "generated_at": None,
        "age_hours": None,
        "evidence_commit_sha": None,
        "evidence_manifest_generated_at": None,
        "decision": None,
        "machine_gates_ready": False,
        "release_decision_status": "missing",
        "local_demo_ready": False,
        "candidate_ready": False,
        "declared_production_ready": False,
        "external_gates": [
            {"key": key, "label": label, "status": "not_reported", "required": ""}
            for key, label in EXPECTED_GATES
        ],
        "release_checks": [],
        "candidate_blockers": [],
        "attachment_count": 0,
        "remote_ci_evidence_complete": False,
    }
    if manifest is None or report is None or check is None:
        return base

    schema_valid = all(
        (
            manifest.get("schema_version") == "delivery-evidence-manifest-v1",
            report.get("schema_version") == "after-sales-release-readiness-v5",
            check.get("schema_version") == "after-sales-delivery-check-v5",
        )
    )
    manifest_time = _timestamp(manifest.get("generated_at"))
    report_time = _timestamp(report.get("generated_at"))
    check_time = _timestamp(check.get("generated_at"))
    if not schema_valid:
        reasons.append("schema_invalid")
    if manifest_time is None or report_time is None or check_time is None:
        reasons.append("generated_at_invalid")
    if reasons:
        base["status"] = "invalid"
        base["reasons"] = list(dict.fromkeys(reasons))
        return base

    assert manifest_time is not None and report_time is not None and check_time is not None
    manifest_files = manifest.get("files")
    bundle_release_sha = _short_text(manifest.get("release_sha"), 80)
    report_release_sha = _short_text(
        (report.get("release_evidence") or {}).get("commit_sha")
        or ((report.get("local_evidence") or {}).get("source_control") or {}).get("commit_sha"),
        80,
    )
    bundle_hashes_valid = isinstance(manifest_files, dict)
    bundle_times_valid = isinstance(manifest_files, dict)
    bundle_attachments_valid = isinstance(manifest_files, dict)
    attachment_times: list[datetime] = []
    attachment_count = 0
    source_report_time = _timestamp(check.get("source_report_generated_at"))
    if source_report_time != report_time or manifest_time < check_time:
        bundle_times_valid = False
    required_payloads = {
        filename: (payload, generated_at)
        for filename, payload, generated_at in (
            (REPORT_FILE, report, report_time),
            (CHECK_FILE, check, check_time),
        )
    }
    known_files = set(required_payloads) | set(REMOTE_CI_ATTACHMENT_CONTRACTS)
    if isinstance(manifest_files, dict) and not set(manifest_files).issubset(
        known_files
    ):
        bundle_attachments_valid = False
    for filename, (payload, generated_at) in required_payloads.items():
        item = manifest_files.get(filename) if isinstance(manifest_files, dict) else None
        if not isinstance(item, dict):
            bundle_hashes_valid = False
            bundle_times_valid = False
            continue
        declared_hash = _short_text(item.get("sha256"), 80).lower()
        actual_hash = _sha256(root / filename)
        if len(declared_hash) != 64 or actual_hash != declared_hash:
            bundle_hashes_valid = False
        declared_time = _timestamp(item.get("generated_at"))
        if declared_time is None or declared_time != generated_at:
            bundle_times_valid = False
        payload_time = _timestamp(payload.get("generated_at"))
        if payload_time != generated_at:
            bundle_times_valid = False
    if isinstance(manifest_files, dict):
        for filename, contract in REMOTE_CI_ATTACHMENT_CONTRACTS.items():
            item = manifest_files.get(filename)
            if item is None:
                continue
            attachment_count += 1
            if not isinstance(item, dict):
                bundle_attachments_valid = False
                continue
            attachment, attachment_error = _read_json(
                root / filename,
                missing_reason=f"{Path(filename).stem}_missing",
            )
            declared_hash = _short_text(item.get("sha256"), 80).lower()
            if len(declared_hash) != 64 or _sha256(root / filename) != declared_hash:
                bundle_hashes_valid = False
            attachment_time = _timestamp(item.get("generated_at"))
            payload_time = _timestamp((attachment or {}).get("generated_at"))
            if attachment_time is None or payload_time != attachment_time:
                bundle_times_valid = False
            elif attachment_time > manifest_time:
                bundle_times_valid = False
            else:
                attachment_times.append(attachment_time)
            if any(
                (
                    attachment_error is not None,
                    item.get("schema_version") != contract["schema_version"],
                    item.get("role") != contract["role"],
                    not remote_ci_attachment_is_safe(filename, attachment),
                )
            ):
                bundle_attachments_valid = False
    if not bundle_hashes_valid:
        reasons.append("evidence_bundle_hash_mismatch")
    if not bundle_times_valid:
        reasons.append("evidence_bundle_generation_mismatch")
    if not bundle_attachments_valid:
        reasons.append("evidence_bundle_attachment_invalid")
    if not bundle_release_sha or bundle_release_sha != report_release_sha:
        reasons.append("evidence_bundle_release_mismatch")
    if reasons:
        base["status"] = "invalid"
        base["reasons"] = list(dict.fromkeys(reasons))
        return base

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    newest_future = max(manifest_time, report_time, check_time)
    if newest_future > current_time + timedelta(minutes=5):
        base["status"] = "invalid"
        base["reasons"] = ["generated_at_in_future"]
        return base

    oldest_time = min(manifest_time, report_time, check_time, *attachment_times)
    age_hours = max(0.0, (current_time - oldest_time).total_seconds() / 3600)
    fresh = age_hours <= max(1, max_age_hours)
    gates, gates_complete = _gate_rows(report)
    evidence_sha = bundle_release_sha
    expected_sha = expected_release_sha.strip()
    if not expected_sha:
        sha_verified: bool | None = None
    else:
        sha_verified = bool(evidence_sha) and (
            evidence_sha == expected_sha
            or evidence_sha.startswith(expected_sha)
            or expected_sha.startswith(evidence_sha)
        )

    if not fresh:
        status = "stale"
        reasons.append("evidence_expired")
    elif sha_verified is None:
        status = "version_unbound"
        reasons.append("runtime_release_sha_missing")
    elif not sha_verified:
        status = "commit_mismatch"
        reasons.append("release_commit_mismatch")
    elif not gates_complete:
        status = "incomplete"
        reasons.append("external_gate_missing")
    else:
        status = "current"

    release_checks = check.get("release_checks")
    safe_checks = [
        {
            "name": _short_text(item.get("name"), 120),
            "status": _short_text(item.get("status"), 40),
            "detail": _short_text(item.get("detail")),
        }
        for item in release_checks[:20]
        if isinstance(item, dict)
    ] if isinstance(release_checks, list) else []
    candidate_blockers = check.get("candidate_blockers")
    safe_blockers = [
        _short_text(item, 120)
        for item in candidate_blockers[:50]
        if isinstance(item, str) and item.strip()
    ] if isinstance(candidate_blockers, list) else []

    report_ready = report.get("production_ready") is True
    check_ready = check.get("production_ready") is True
    machine_ready = (report.get("release_evidence") or {}).get("machine_gates_ready") is True
    release_decision = report.get("release_decision") or {}
    approval_ready = (
        release_decision.get("status") == "approved"
        and release_decision.get("gate_status") == "verified_human"
    )
    all_external_verified = all(item["status"] == "verified_external" for item in gates)
    trusted_ready = all(
        (
            status == "current",
            report_ready,
            check_ready,
            machine_ready,
            approval_ready,
            all_external_verified,
        )
    )

    return {
        **base,
        "status": status,
        "evidence_valid": True,
        "evidence_bundle_verified": True,
        "evidence_fresh": fresh,
        "release_sha_verified": sha_verified,
        "trusted_production_ready": trusted_ready,
        "reasons": list(dict.fromkeys(reasons)),
        "generated_at": oldest_time.isoformat(),
        "age_hours": round(age_hours, 2),
        "evidence_commit_sha": evidence_sha[:12] or None,
        "evidence_manifest_generated_at": manifest_time.isoformat(),
        "decision": _short_text(report.get("decision"), 80) or None,
        "machine_gates_ready": machine_ready,
        "release_decision_status": _short_text(release_decision.get("status"), 80) or "missing",
        "local_demo_ready": check.get("local_demo_ready") is True,
        "candidate_ready": check.get("candidate_ready") is True,
        "declared_production_ready": report_ready and check_ready,
        "external_gates": gates,
        "release_checks": safe_checks,
        "candidate_blockers": safe_blockers,
        "attachment_count": attachment_count,
        "remote_ci_evidence_complete": set(REMOTE_CI_ATTACHMENT_CONTRACTS).issubset(
            manifest_files
        ),
    }


__all__ = ["build_delivery_readiness_snapshot"]

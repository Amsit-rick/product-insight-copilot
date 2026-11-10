"""Verify a delivery evidence artifact without granting release approval."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.delivery_readiness import build_delivery_readiness_snapshot


def verify_delivery_evidence_bundle(
    evidence_dir: str | Path,
    *,
    expected_release_sha: str,
    max_age_hours: int = 24,
    now: datetime | None = None,
) -> dict[str, Any]:
    expected_sha = expected_release_sha.strip()
    if not expected_sha:
        return {
            "schema_version": "delivery-evidence-verification-v1",
            "verified": False,
            "status": "invalid",
            "evidence_bundle_verified": False,
            "evidence_fresh": False,
            "release_sha_verified": False,
            "trusted_production_ready": False,
            "evidence_commit_sha": None,
            "generated_at": None,
            "attachment_count": 0,
            "remote_ci_evidence_complete": False,
            "reasons": ["expected_release_sha_missing"],
        }

    snapshot = build_delivery_readiness_snapshot(
        evidence_dir,
        expected_release_sha=expected_sha,
        max_age_hours=max_age_hours,
        now=now,
    )
    verified = all(
        (
            snapshot.get("status") == "current",
            snapshot.get("evidence_valid") is True,
            snapshot.get("evidence_bundle_verified") is True,
            snapshot.get("evidence_fresh") is True,
            snapshot.get("release_sha_verified") is True,
        )
    )
    return {
        "schema_version": "delivery-evidence-verification-v1",
        "verified": verified,
        "status": snapshot.get("status"),
        "evidence_bundle_verified": snapshot.get("evidence_bundle_verified") is True,
        "evidence_fresh": snapshot.get("evidence_fresh") is True,
        "release_sha_verified": snapshot.get("release_sha_verified"),
        "trusted_production_ready": snapshot.get("trusted_production_ready") is True,
        "evidence_commit_sha": snapshot.get("evidence_commit_sha"),
        "generated_at": snapshot.get("generated_at"),
        "attachment_count": int(snapshot.get("attachment_count") or 0),
        "remote_ci_evidence_complete": snapshot.get("remote_ci_evidence_complete") is True,
        "reasons": list(snapshot.get("reasons") or []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线复验交付证据包的哈希、批次、时效与发布 SHA")
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--max-age-hours", type=int, default=24)
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)
    result = verify_delivery_evidence_bundle(
        args.evidence_dir,
        expected_release_sha=args.expected_release_sha,
        max_age_hours=args.max_age_hours,
    )
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    print(encoded)
    if args.json_out:
        output = Path(args.json_out).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
        print(f"交付证据复验摘要={output}")
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

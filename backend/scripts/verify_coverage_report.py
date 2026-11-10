"""Verify statement and branch coverage from a coverage.py JSON report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class CoverageVerificationError(ValueError):
    """Raised when coverage evidence is missing or structurally invalid."""


def _normalize_path(value: str) -> str:
    return value.replace("\\", "/").removeprefix("./").strip("/")


def _required_count(summary: dict[str, Any], key: str) -> int:
    value = summary.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CoverageVerificationError(f"覆盖率字段 {key} 必须是非负整数")
    return value


def _percentage(covered: int, total: int, *, label: str) -> float:
    if total <= 0:
        raise CoverageVerificationError(f"{label}覆盖率分母必须大于 0")
    if covered > total:
        raise CoverageVerificationError(f"{label}覆盖数不能大于总数")
    return round(covered / total * 100, 2)


def verify_coverage_report(
    coverage_json: Path,
    *,
    target_file: str,
    min_statements: float,
    min_branches: float,
) -> dict[str, Any]:
    if not 0 <= min_statements <= 100 or not 0 <= min_branches <= 100:
        raise CoverageVerificationError("覆盖率阈值必须在 0 到 100 之间")
    try:
        payload = json.loads(coverage_json.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise CoverageVerificationError("覆盖率 JSON 不存在") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CoverageVerificationError("覆盖率 JSON 无法读取") from exc
    if not isinstance(payload, dict):
        raise CoverageVerificationError("覆盖率 JSON 顶层必须是对象")

    meta = payload.get("meta")
    if not isinstance(meta, dict) or meta.get("branch_coverage") is not True:
        raise CoverageVerificationError("覆盖率报告未启用分支覆盖统计")
    files = payload.get("files")
    if not isinstance(files, dict):
        raise CoverageVerificationError("覆盖率报告缺少 files 对象")

    normalized_target = _normalize_path(target_file)
    matches = [
        (name, item)
        for name, item in files.items()
        if isinstance(name, str)
        and (
            _normalize_path(name) == normalized_target
            or _normalize_path(name).endswith(f"/{normalized_target}")
        )
    ]
    if len(matches) != 1:
        raise CoverageVerificationError(
            f"覆盖率报告中目标文件匹配数量应为 1，实际为 {len(matches)}"
        )

    matched_name, file_payload = matches[0]
    if not isinstance(file_payload, dict) or not isinstance(
        file_payload.get("summary"), dict
    ):
        raise CoverageVerificationError("目标文件缺少 summary 对象")
    summary = file_payload["summary"]
    covered_statements = _required_count(summary, "covered_lines")
    num_statements = _required_count(summary, "num_statements")
    covered_branches = _required_count(summary, "covered_branches")
    num_branches = _required_count(summary, "num_branches")
    statement_percent = _percentage(
        covered_statements, num_statements, label="语句"
    )
    branch_percent = _percentage(covered_branches, num_branches, label="分支")
    checks = {
        "statements": statement_percent >= min_statements,
        "branches": branch_percent >= min_branches,
    }
    return {
        "schema_version": "coverage-threshold-verification-v1",
        "status": "passed" if all(checks.values()) else "failed",
        "target_file": _normalize_path(matched_name),
        "statement_percent": statement_percent,
        "branch_percent": branch_percent,
        "thresholds": {
            "statements": min_statements,
            "branches": min_branches,
        },
        "counts": {
            "covered_statements": covered_statements,
            "num_statements": num_statements,
            "covered_branches": covered_branches,
            "num_branches": num_branches,
        },
        "checks": checks,
        "production_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 coverage.py 语句和分支覆盖率")
    parser.add_argument("--coverage-json", required=True)
    parser.add_argument("--file", required=True, dest="target_file")
    parser.add_argument("--min-statements", required=True, type=float)
    parser.add_argument("--min-branches", required=True, type=float)
    args = parser.parse_args(argv)
    try:
        result = verify_coverage_report(
            Path(args.coverage_json).resolve(),
            target_file=args.target_file,
            min_statements=args.min_statements,
            min_branches=args.min_branches,
        )
    except CoverageVerificationError as exc:
        print(f"覆盖率门禁证据无效：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "verify_coverage_report.py"


def _load_module():
    assert SCRIPT.exists(), "缺少结构化覆盖率门禁脚本"
    spec = importlib.util.spec_from_file_location("verify_coverage_report_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_report(
    path: Path,
    *,
    statements: tuple[int, int] = (87, 100),
    branches: tuple[int, int] = (81, 100),
) -> None:
    path.write_text(
        json.dumps(
            {
                "meta": {"branch_coverage": True},
                "files": {
                    "scripts\\after_sales_release_report.py": {
                        "summary": {
                            "covered_lines": statements[0],
                            "num_statements": statements[1],
                            "covered_branches": branches[0],
                            "num_branches": branches[1],
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_coverage_report_requires_statement_and_branch_thresholds(tmp_path):
    module = _load_module()
    path = tmp_path / "coverage.json"
    _write_report(path)

    result = module.verify_coverage_report(
        path,
        target_file="scripts/after_sales_release_report.py",
        min_statements=80,
        min_branches=80,
    )

    assert result["status"] == "passed"
    assert result["statement_percent"] == 87.0
    assert result["branch_percent"] == 81.0
    assert result["production_ready"] is False


def test_coverage_report_fails_when_branch_threshold_is_missed(tmp_path):
    module = _load_module()
    path = tmp_path / "coverage.json"
    _write_report(path, branches=(79, 100))

    result = module.verify_coverage_report(
        path,
        target_file="scripts/after_sales_release_report.py",
        min_statements=80,
        min_branches=80,
    )

    assert result["status"] == "failed"
    assert result["checks"] == {"statements": True, "branches": False}


def test_coverage_report_rejects_missing_branch_measurement(tmp_path):
    module = _load_module()
    path = tmp_path / "coverage.json"
    _write_report(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["meta"]["branch_coverage"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(module.CoverageVerificationError, match="分支覆盖"):
        module.verify_coverage_report(
            path,
            target_file="scripts/after_sales_release_report.py",
            min_statements=80,
            min_branches=80,
        )

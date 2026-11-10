"""本地售后候选版本交付检查。"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "after_sales_delivery_check.py"
SPEC = importlib.util.spec_from_file_location("after_sales_delivery_check", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def _report(**overrides):
    report = {
        "schema_version": "after-sales-release-readiness-v5",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"adapter_mode": "mock"},
        "local_evidence": {
            "preflight": {"normal": {"fail": 0}, "strict": {"fail": 0, "failed_checks": []}},
            "fact_probe_plan": {"status": "pass"},
            "demo_tickets": {"status": "pass"},
            "production_shape": {
                "local_production_shape_verified": True,
                "cross_host_or_target_environment_verified": False,
            },
            "usability_test": {"template_valid": True, "status": "template_valid_unfilled"},
            "source_control": {"traceable": True, "blockers": []},
        },
        "external_gates": [{"key": "usability_test", "status": "blocked_external"}],
        "infrastructure_verification": {
            "cross_host_or_target_environment_verified": False
        },
        "release_evidence": {
            "machine_gates_ready": False,
            "target_release_admission_verified": False,
        },
        "production_ready": False,
    }
    report.update(overrides)
    return report


def test_mock_report_is_local_demo_ready_but_not_release_candidate(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report(), ensure_ascii=False), encoding="utf-8")

    result = module.build_delivery_check(path)

    assert result["local_demo_ready"] is True
    assert result["candidate_ready"] is False
    assert result["production_ready"] is False
    assert result["mock_mode"] is True
    assert result["external_blocked_gates"] == ["usability_test"]
    assert result["candidate_blockers"] == [
        "target_environment",
        "mock_fact_adapters",
        "usability_test",
        "machine_gates",
        "target_release_admission",
        "release_approval",
    ]
    assert any(item["name"] == "版本可追溯" and item["status"] == "PASS" for item in result["release_checks"])
    assert result["local_production_shape_verified"] is True
    assert result["cross_host_or_target_environment_verified"] is False


def test_real_production_report_can_be_release_candidate(tmp_path):
    """非 mock 且生产与外部门禁均通过时，candidate_ready 必须有可达的正向路径。"""
    path = tmp_path / "report.json"
    report = _report(
        environment={"adapter_mode": "http"},
        infrastructure_verification={
            "cross_host_or_target_environment_verified": True
        },
        external_gates=[],
        release_evidence={
            "machine_gates_ready": True,
            "target_release_admission_verified": True,
        },
        release_decision={
            "status": "approved",
            "gate_status": "verified_human",
            "approver_role": "release_manager",
            "commit_matches": True,
            "evidence_matches": True,
            "rollback_confirmed": True,
        },
        production_ready=True,
    )
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    result = module.build_delivery_check(path)

    assert result["local_demo_ready"] is False
    assert result["candidate_ready"] is True
    assert result["production_ready"] is True
    assert result["mock_mode"] is False
    assert result["adapter_mode"] == "http"
    assert result["candidate_blockers"] == []
    assert result["cross_host_or_target_environment_verified"] is True
    assert next(
        item for item in result["release_checks"] if item["name"] == "目标环境多节点"
    )["status"] == "PASS"
    assert next(
        item for item in result["release_checks"] if item["name"] == "机器发布门禁"
    )["status"] == "PASS"
    assert next(
        item for item in result["release_checks"] if item["name"] == "统一候选准入"
    )["status"] == "PASS"
    assert next(
        item for item in result["release_checks"] if item["name"] == "人工发布批准"
    )["status"] == "PASS"


def test_dirty_source_and_strict_failures_are_explicit_candidate_blockers(tmp_path):
    path = tmp_path / "report.json"
    local_evidence = _report()["local_evidence"]
    local_evidence["preflight"]["strict"] = {
        "fail": 2,
        "failed_checks": ["JWT 密钥", "任务执行"],
    }
    local_evidence["source_control"] = {
        "traceable": False,
        "blockers": ["working_tree_dirty"],
    }
    path.write_text(
        json.dumps(
            _report(
                environment={"adapter_mode": "http"},
                local_evidence=local_evidence,
                external_gates=[],
                production_ready=False,
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = module.build_delivery_check(path)

    assert result["local_demo_ready"] is False
    assert result["candidate_ready"] is False
    assert result["candidate_blockers"] == [
        "strict_preflight",
        "version_traceability",
        "target_environment",
        "machine_gates",
        "target_release_admission",
        "release_approval",
    ]
    assert next(item for item in result["release_checks"] if item["name"] == "严格 preflight")["detail"] == "FAIL=2：JWT 密钥、任务执行"
    assert next(item for item in result["release_checks"] if item["name"] == "版本可追溯")["detail"] == "working_tree_dirty"


def test_stale_report_is_not_candidate_ready(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report(generated_at="2020-01-01T00:00:00+00:00"), ensure_ascii=False), encoding="utf-8")

    result = module.build_delivery_check(path, max_age_hours=24)

    assert result["candidate_ready"] is False
    assert result["local_demo_ready"] is False
    assert any(item["name"] == "报告新鲜度" and item["status"] == "FAIL" for item in result["checks"])


def test_missing_report_is_not_candidate_ready(tmp_path):
    result = module.build_delivery_check(tmp_path / "missing.json")

    assert result["candidate_ready"] is False
    assert result["local_demo_ready"] is False
    assert result["production_ready"] is False


def test_non_positive_report_age_is_rejected(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report(), ensure_ascii=False), encoding="utf-8")

    result = module.build_delivery_check(path, max_age_hours=0)

    assert result["candidate_ready"] is False
    assert result["local_demo_ready"] is False
    assert result["checks"][0]["name"] == "报告新鲜度参数"


def test_machine_gate_cannot_bypass_missing_target_release_admission(tmp_path):
    path = tmp_path / "report.json"
    report = _report(
        environment={"adapter_mode": "http"},
        infrastructure_verification={
            "cross_host_or_target_environment_verified": True
        },
        external_gates=[],
        release_evidence={
            "machine_gates_ready": True,
            "target_release_admission_verified": False,
        },
    )
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    result = module.build_delivery_check(path)

    assert result["candidate_ready"] is False
    assert "target_release_admission" in result["candidate_blockers"]

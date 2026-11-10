"""结构化客服测试记录的格式与脱敏边界。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "validate_usability_test_record.py"
SPEC = importlib.util.spec_from_file_location("validate_usability_test_record", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_blank_template_has_required_participants_and_tasks():
    payload = module.template_payload()
    summary, errors = module.validate_record(payload)

    assert errors == []
    assert summary is not None
    assert summary["participant_count"] == 5
    assert summary["task_count"] == 18
    assert summary["independent_completion_rate"] is None


def test_filled_record_returns_observed_summary_without_user_conclusion():
    payload = module.template_payload()
    payload["tasks"][0]["completed"] = True
    payload["tasks"][0]["independent"] = True
    payload["tasks"][0]["duration_seconds"] = 42
    payload["issues"] = [{"severity": "P1", "description": "多人找不到下一步"}]

    summary, errors = module.validate_record(payload)

    assert errors == []
    assert summary is not None
    assert summary["completed_task_count"] == 1
    assert summary["independent_completion_rate"] == 1.0
    assert summary["issue_counts"] == {"P0": 0, "P1": 1, "P2": 0}
    assert summary["note"]


def test_sensitive_fields_are_rejected():
    payload = module.template_payload()
    payload["participants"][0]["password"] = "ops123"

    summary, errors = module.validate_record(payload)

    assert summary is None
    assert any("敏感字段" in error for error in errors)


def test_incomplete_or_duplicate_task_matrix_is_rejected():
    payload = module.template_payload()
    payload["tasks"] = payload["tasks"][:-1]
    payload["tasks"].append(dict(payload["tasks"][0]))

    summary, errors = module.validate_record(payload)

    assert summary is None
    assert any("重复" in error for error in errors)
    assert any("缺少任务记录" in error for error in errors)


def test_complete_non_developer_record_exposes_gate_inputs_without_deciding_product_readiness():
    payload = module.template_payload()
    payload["test_date"] = "2026-11-07"
    payload["facilitator"] = "external-facilitator"
    payload["participant_scope_confirmed"] = True
    payload["conclusion"] = "已完成观察，是否交付由发布门禁综合判断。"
    for participant in payload["participants"]:
        participant["is_developer"] = False
    for task in payload["tasks"]:
        task.update(
            {
                "completed": True,
                "independent": True,
                "duration_seconds": 60,
            }
        )

    summary, errors = module.validate_record(payload)

    assert errors == []
    assert summary is not None
    assert summary["fully_observed_task_count"] == 18
    assert summary["all_tasks_fully_observed"] is True
    assert summary["non_developer_confirmed_count"] == 5
    assert summary["participant_scope_confirmed"] is True
    assert summary["unresolved_issue_counts"] == {"P0": 0, "P1": 0, "P2": 0}

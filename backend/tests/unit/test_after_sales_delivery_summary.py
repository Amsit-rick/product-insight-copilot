"""售后候选版本交付摘要的诚实表达。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "after_sales_delivery_summary.py"
SPEC = importlib.util.spec_from_file_location("after_sales_delivery_summary", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_summary_distinguishes_candidate_from_production():
    report = {
        "local_evidence": {
            "preflight": {"normal": {"fail": 0}},
            "fact_probe_plan": {"status": "pass"},
            "demo_tickets": {"status": "pass", "present_count": 3, "required_count": 3},
            "policy_retrieval_benchmark": {
                "policy_retrieval_verified": True,
                "case_count": 8,
                "recall_at_k": 1.0,
            },
            "usability_test": {
                "status": "template_valid_unfilled",
                "template_valid": True,
                "observed_task_count": 0,
                "independent_completion_rate": None,
            },
        },
        "infrastructure_verification": {
            "cross_host_or_target_environment_verified": True
        },
        "external_gates": [
            {"key": "metric_source", "status": "blocked_external", "required": "真实指标 API"},
            {"key": "real_model_holdout", "status": "blocked_external", "required": "真实模型 holdout"},
        ],
        "release_decision": {"status": "missing"},
    }
    check = {
        "local_demo_ready": True,
        "candidate_ready": False,
        "production_ready": False,
        "mock_mode": True,
        "candidate_blockers": ["strict_preflight", "version_traceability", "real_model_holdout"],
        "release_checks": [
            {"name": "严格 preflight", "status": "FAIL", "detail": "FAIL=2：JWT 密钥、任务执行"},
            {"name": "版本可追溯", "status": "FAIL", "detail": "working_tree_dirty"},
        ],
    }

    rendered = module.build_summary(report, check)

    assert "本地演示版本：**可演示**" in rendered
    assert "发布候选版本：**不可交付**" in rendered
    assert "生产版本：**未验收**" in rendered
    assert "人工发布批准：**等待授权人员审批**" in rendered
    assert "状态=missing" not in rendered
    assert "本地 mock/演示" in rendered
    assert "真实模型 holdout" in rendered
    assert "真实观察=0 条，完成率=未填写" in rendered
    assert "目标环境多节点 | 通过" in rendered
    assert "严格 preflight" in rendered
    assert "FAIL=2：JWT 密钥、任务执行" in rendered
    assert "版本可追溯" in rendered
    assert "工作区存在未提交或未跟踪改动" in rendered
    assert "政策检索 Recall@5 | 通过" in rendered
    assert "真实业务指标源 | 待外部验证 | 真实指标 API" in rendered
    assert "配置生产 HTTPS 指标 API" in rendered


def test_summary_does_not_invent_external_success():
    report = {
        "local_evidence": {},
        "external_gates": [{"key": "sandbox", "status": "blocked_external", "required": "真实 sandbox"}],
    }
    check = {
        "local_demo_ready": False,
        "candidate_ready": False,
        "production_ready": False,
        "mock_mode": True,
    }

    rendered = module.build_summary(report, check)

    assert "真实 sandbox" in rendered
    assert "生产版本：**未验收**" in rendered


def test_summary_reports_unknown_mode_when_mock_flag_is_missing():
    rendered = module.build_summary(
        {"local_evidence": {}, "external_gates": []},
        {"local_demo_ready": False, "candidate_ready": False, "production_ready": False},
    )

    assert "运行模式：**运行模式未知**" in rendered
    assert "真实适配器" not in rendered


def test_summary_marks_verified_external_gate_without_repeating_it_as_blocker():
    rendered = module.build_summary(
        {
            "local_evidence": {},
            "external_gates": [
                {
                    "key": "metric_source",
                    "status": "verified_external",
                    "required": "生产指标源",
                }
            ],
        },
        {"local_demo_ready": True, "candidate_ready": False, "production_ready": False},
    )

    assert "真实业务指标源 | 已验证" in rendered
    assert "- 生产指标源" not in rendered

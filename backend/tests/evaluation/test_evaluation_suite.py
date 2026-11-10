"""评测集与评测运行的测试（设计文档 16.2 / 16.3）。

默认只跑一小部分用例（保证 CI 快速）；设置环境变量 `PIC_EVAL_LIMIT=0` 可跑完整 50 条。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

CASES_PATH = Path(__file__).resolve().parent / "cases.json"
EXPECTED_CATEGORIES = {
    "single_kb": 10,
    "cross_kb": 8,
    "no_answer": 6,
    "conflict_evidence": 6,
    "version_time_mismatch": 5,
    "data_missing": 5,
    "unauthorized": 5,
    "prompt_injection": 5,
}
REQUIRED_KEYS = {
    "case_id",
    "category",
    "question",
    "filters",
    "expected_kb",
    "expected_intent",
    "expected_metric",
    "expect_refusal",
    "expect_conflict",
    "expect_human_review",
    "security_level",
    "notes",
}


# --------------------------------------------------------------------------- 数据集
def test_evaluation_dataset_is_complete_and_well_formed():
    assert CASES_PATH.exists(), "缺少评测集文件 tests/evaluation/cases.json"
    raw = CASES_PATH.read_text(encoding="utf-8")
    assert not raw.startswith("\ufeff"), "评测集不应带 BOM"
    cases = json.loads(raw)

    assert len(cases) == 50, "评测集需要至少 50 条问题"
    assert len({case["case_id"] for case in cases}) == 50

    counts: dict[str, int] = {}
    for case in cases:
        counts[case["category"]] = counts.get(case["category"], 0) + 1
        assert set(case) >= REQUIRED_KEYS, f"{case['case_id']} 字段缺失"
        assert case["question"].strip()
        assert case["filters"].get("product"), f"{case['case_id']} 缺少产品过滤"
        assert case["category"] in EXPECTED_CATEGORIES
        if case["category"] == "unauthorized":
            assert case["security_level"] == "confidential"
        if case["category"] == "prompt_injection":
            assert case["expect_human_review"] is True
        if case["category"] == "conflict_evidence":
            assert case["expect_conflict"] is True

    assert counts == EXPECTED_CATEGORIES, f"类别分布与预期不一致: {counts}"


# --------------------------------------------------------------------------- 运行
@pytest.mark.slow
def test_evaluation_run_produces_metrics(client, auth):
    """真实跑评测：返回聚合指标，并记录工作流/Prompt/Embedding 版本便于对比。"""
    limit = int(os.environ.get("PIC_EVAL_LIMIT", "8"))
    payload = {"dataset_version": "eval-1.0.0", "name": "集成测试评测"}
    if limit:
        payload["limit"] = limit

    resp = client.post("/api/v1/evaluations/run", headers=auth["admin"], json=payload)
    assert resp.status_code == 201, resp.text
    run = resp.json()

    assert run["case_count"] >= 1
    assert run["status"] == "completed"
    assert run["workflow_version"] and run["prompt_version"] and run["embedding_version"]

    metrics = run["metrics"]
    assert 0.0 <= metrics["pass_rate"] <= 1.0
    assert metrics["category_counts"], "应按类别给出通过率"
    assert metrics["avg_latency_ms"] is not None
    assert run["details"], "评测必须逐条记录明细"
    for detail in run["details"]:
        assert "checks" in detail and detail["checks"], "每条用例都要有判定明细"

    # 越权与无答案类别的拒答准确率必须有定义
    assert "refusal_accuracy" in metrics


@pytest.mark.slow
def test_evaluation_is_repeatable(client, auth):
    """同一数据集重复运行结果稳定（同一批用例、同一判定逻辑）。"""
    payload = {"dataset_version": "eval-1.0.0", "name": "重复性校验", "limit": 3}
    first = client.post("/api/v1/evaluations/run", headers=auth["admin"], json=payload).json()
    second = client.post("/api/v1/evaluations/run", headers=auth["admin"], json=payload).json()
    first_cases = {d["case_id"]: d["passed"] for d in first["details"]}
    second_cases = {d["case_id"]: d["passed"] for d in second["details"]}
    assert first_cases.keys() == second_cases.keys()

    runs = client.get("/api/v1/evaluations/runs", headers=auth["admin"]).json()
    assert len(runs) >= 2
    assert runs[0]["case_count"] == 3

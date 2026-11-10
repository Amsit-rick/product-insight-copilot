"""售后工单评测的指标计算与数据集校验单测。

指标算错比功能出错更危险：它会让"看起来达标"掩盖真实问题。所以这里用**手算可核对的样例**
钉住 Macro-F1、等级比较、数据集覆盖矩阵与期望字段的合法性。
"""

from __future__ import annotations

import json

from app.services.ticket_evaluation_service import (
    FORBIDDEN_POLICY_VERSIONS,
    TicketEvaluationService,
    _case_cost,
    _evaluation_case_key,
    _macro_f1,
    _tier_rank,
)
from app.config import Settings


def _detail(group: str, expect: dict, observed: dict) -> dict:
    return {"group": group, "expect": expect, "observed": observed, "checks": {}, "passed": True}


def test_tier_rank_orders_levels():
    assert _tier_rank("L1") < _tier_rank("L2") < _tier_rank("L3") < _tier_rank("L4")
    # 未知等级按 0 处理：出现在判定里会让"未知"被当成最低风险，所以测试也钉住它
    assert _tier_rank(None) == 0
    assert _tier_rank("XX") == 0


def test_macro_f1_perfect_and_partial():
    perfect = [
        _detail("normal", {"issue_type": "refund"}, {"issue_type": "refund"}),
        _detail("normal", {"issue_type": "damaged"}, {"issue_type": "damaged"}),
    ]
    assert _macro_f1(perfect) == 1.0

    # 两条用例、三个类别：refund 分类全对（F1=1），damaged 漏召（F1=0），
    # 错判出来的 missing_item 精确率 0（F1=0）→ Macro-F1 = (1+0+0)/3
    mixed = [
        _detail("normal", {"issue_type": "refund"}, {"issue_type": "refund"}),
        _detail("normal", {"issue_type": "damaged"}, {"issue_type": "missing_item"}),
    ]
    assert _macro_f1(mixed) == round(1 / 3, 4)

    # 三条用例、三个类别，逐类手算：
    #   refund        tp=1 fp=0 fn=0 → P=1.0   R=1.0   F1=1.0
    #   damaged       tp=1 fp=1 fn=0 → P=0.5   R=1.0   F1=0.6667（missing_item 那条被误判成 damaged）
    #   missing_item  tp=0 fp=0 fn=1 → P=0     R=0     F1=0
    # Macro-F1 = (1 + 0.6667 + 0) / 3 = 0.5556
    partial = [
        _detail("normal", {"issue_type": "refund"}, {"issue_type": "refund"}),
        _detail("normal", {"issue_type": "damaged"}, {"issue_type": "damaged"}),
        _detail("normal", {"issue_type": "missing_item"}, {"issue_type": "damaged"}),
    ]
    value = _macro_f1(partial)
    assert value is not None and abs(value - 0.5556) < 1e-6


def test_macro_f1_ignores_cases_without_labels():
    assert _macro_f1([_detail("normal", {}, {"issue_type": "refund"})]) is None


def test_real_model_cost_uses_each_agent_actual_model_and_token_shape():
    settings = Settings(
        _env_file=None,
        model_prices={
            "model-a": {"input": 2.0, "output": 8.0, "cache_hit_input": 0.5},
            "model-b": {"input": 4.0, "output": 10.0, "cache_hit_input": 1.0},
        },
        model_price_default={"input": 20.0, "output": 20.0},
    )
    detail = {
        "observed": {
            "usage_records": [
                {
                    "model": "model-a",
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cache_hit_tokens": 400,
                    "model_used": True,
                },
                {
                    "model": "model-b",
                    "input_tokens": 500,
                    "output_tokens": 100,
                    "cache_hit_tokens": 0,
                    "model_used": True,
                },
                {
                    "model": "stub",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_hit_tokens": 0,
                    "model_used": False,
                },
            ]
        }
    }

    # model-a: (600*2 + 400*0.5 + 200*8)/1e6 = 0.003
    # model-b: (500*4 + 100*10)/1e6 = 0.003
    assert _case_cost(detail, settings) == 0.006


def test_repeated_evaluation_runs_use_different_ticket_idempotency_keys():
    first = _evaluation_case_key("run-a", "case-001")
    second = _evaluation_case_key("run-b", "case-001")

    assert first != second
    assert first == _evaluation_case_key("run-a", "case-001")
    assert len(first) == 24


def test_dataset_covers_required_dimensions():
    """需求书 §9 的验收维度必须都在数据集里 —— 少了就等于没测。"""
    cases = TicketEvaluationService.load_cases()
    groups = {case["group"] for case in cases}
    required = {
        "normal",  # 正常工单
        "refund",  # L3 风险
        "risk_flag",  # L4 风险
        "missing_order",  # 缺少订单号
        "order_not_found",  # 订单不存在
        "adapter_timeout",  # 物流接口超时
        "unauthorized",  # 租户/知识库越权
        "duplicate_decision",  # 重复审批请求
        "low_confidence",  # 低置信度
    }
    assert required <= groups, f"缺少验收维度：{required - groups}"
    # tickets-1.1.0 新增的三个维度（都不需要新的判定字段，理由见生成器文档字符串）
    assert {"cross_shop", "fact_variant", "escalation_wording"} <= groups, "缺少新增维度"
    assert len(cases) >= 200, f"用例数应不少于 200，当前 {len(cases)}"


def test_dataset_group_matrix_meets_planned_minimums():
    """覆盖矩阵的每一格都要够数：扩集不能靠"把 normal 灌满"来凑总数。

    这里的数字是**下限**（生成器文档字符串里那张表的口径），后续继续扩集不会把测试改坏，
    但谁要是把某个维度悄悄削回去（例如把破损组从 10 条砍到 3 条），这里会立刻红。
    """
    cases = TicketEvaluationService.load_cases()
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["group"]] = counts.get(case["group"], 0) + 1
    minimums = {
        "normal": 80,  # 10 类问题 × 8 变体
        "refund": 12,
        "risk_flag": 8,
        "sensitive": 6,
        "promotion": 10,
        "damaged": 10,
        "stalled": 12,
        "stalled_short": 6,
        "reship": 12,
        "missing_order": 8,
        "order_not_found": 6,
        "adapter_timeout": 6,
        "unauthorized": 6,
        "duplicate_decision": 6,
        "low_confidence": 6,
        "cross_shop": 12,
        "fact_variant": 12,
        "escalation_wording": 10,
    }
    short = {group: (counts.get(group, 0), want) for group, want in minimums.items() if counts.get(group, 0) < want}
    assert not short, f"这些分组没达到计划条数（当前, 期望）：{short}"


def test_dataset_inputs_are_unique():
    """输入三元组（subject, content, order_id）必须唯一 —— 重复条目等于虚报覆盖度。

    扩集最容易走的路就是"复制一条改个数字"：那样条数涨了，信息量没涨，
    而且会把指标往"同一句话反复答对"的方向拉偏。这里按**输入**去重（不是按 case_id），
    所以只有真正换了措辞/换了订单事实的用例才算新增。
    """
    cases = TicketEvaluationService.load_cases()
    triples = [(case["subject"], case["content"], case.get("order_id")) for case in cases]
    duplicates = {item for item in triples if triples.count(item) > 1}
    assert not duplicates, f"存在完全相同的输入三元组：{sorted(duplicates)[:5]}"

    # 更强的一条：（content, order_id）也不得重复 —— 换掉 subject 但输入一模一样，
    # 只是把同一次判定数两遍。允许的唯一例外是"同一句话 × 不同订单事实"那组：
    # 它们共用原话，但订单事实必须不同，否则这个维度就没有意义。
    inputs = [(case["content"], case.get("order_id")) for case in cases]
    repeated = {item for item in inputs if inputs.count(item) > 1}
    assert not repeated, f"同一段原话配同一份订单事实被判定多次：{sorted(repeated)[:5]}"


def test_dataset_has_no_duplicate_ids_and_valid_expectations():
    cases = TicketEvaluationService.load_cases()
    ids = [case["case_id"] for case in cases]
    assert len(ids) == len(set(ids)), "用例 ID 必须唯一"

    known_tiers = {"L1", "L2", "L3", "L4"}
    for case in cases:
        expect = case.get("expect") or {}
        for key in ("risk_tier", "max_risk_tier", "min_risk_tier"):
            if key in expect:
                assert expect[key] in known_tiers, f"{case['case_id']} 的 {key} 非法：{expect[key]}"
        # 未标注期望的用例等于白跑
        assert expect, f"{case['case_id']} 没有期望，无法判定"


def test_unauthorized_cases_always_expect_denial():
    for case in TicketEvaluationService.load_cases():
        if case["group"] != "unauthorized":
            continue
        assert case["expect"].get("error_code") == "permission_denied"
        assert case.get("request_knowledge_bases"), "越权用例必须声明要访问哪个库"


def test_forbidden_policy_versions_are_the_expired_and_future_ones():
    """泄漏检测集合必须覆盖演示数据里"已失效/未生效"的两个版本。"""
    assert FORBIDDEN_POLICY_VERSIONS == {"v1.0", "v4.0"}


def _generate_cases() -> list[dict]:
    """直接调用生成器拿用例（不落盘）：JSON 与生成器必须逐字节一致。"""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "generate_ticket_eval_cases.py"
    spec = importlib.util.spec_from_file_location("generate_ticket_eval_cases", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_cases()


def test_dataset_file_is_reproducible():
    """生成器与提交的 JSON 必须一致：改数据集要经过生成器，避免手改导致覆盖矩阵失真。

    这里比对**全部用例内容**而不是仅仅条数：只比条数的话，手改一条期望值照样能过。
    """
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "evaluation" / "tickets.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["dataset_version"] == "tickets-1.1.1"
    generated = _generate_cases()
    assert payload["cases"] == generated, "tickets.json 与生成器输出不一致：请重新执行 generate_ticket_eval_cases.py"
    assert len(payload["cases"]) == len(TicketEvaluationService.load_cases())


def test_stratified_sample_covers_all_groups_and_is_deterministic():
    """真实模型小样本评测不能只取前 N 条（前 50 条全是 normal）。"""

    sampled = TicketEvaluationService.load_cases(sample=50)
    assert len(sampled) == 50
    assert len({case["case_id"] for case in sampled}) == 50
    assert len({case["group"] for case in sampled}) == 18, "分层抽样应覆盖全部 18 个验收维度"
    assert sampled == TicketEvaluationService.load_cases(sample=50), "抽样结果必须可复现"

    one_per_group = TicketEvaluationService.load_cases(sample=18)
    assert len({case["group"] for case in one_per_group}) == 18

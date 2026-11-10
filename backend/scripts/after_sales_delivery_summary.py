"""Render a concise, honest handoff summary from after-sales evidence artifacts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "testing" / "artifacts" / "after-sales-release-readiness.json"
CHECK = ROOT / "docs" / "testing" / "artifacts" / "after-sales-delivery-check.json"


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _status(value: Any) -> str:
    return "通过" if value else "未通过"


def _display(value: Any) -> str:
    return "未填写" if value is None else str(value)


SOURCE_BLOCKER_LABELS = {
    "repository_unavailable": "当前目录不是可读取的 Git 仓库",
    "commit_unavailable": "无法读取提交 SHA",
    "working_tree_dirty": "工作区存在未提交或未跟踪改动",
    "upstream_missing": "当前分支没有上游分支",
    "upstream_not_synced": "当前提交与上游分支未同步",
}

EXTERNAL_GATE_LABELS = {
    "fact_platform_sandbox": "订单、支付、物流与售后事实接口",
    "metric_source": "真实业务指标源",
    "message_channel": "站外消息通知渠道",
    "production_infrastructure": "生产部署与故障切换",
    "real_model_holdout": "真实模型独立评测",
    "usability_test": "客服与主管真人可用性",
}

EXTERNAL_GATE_ACTIONS = {
    "fact_platform_sandbox": "接入隔离 sandbox 和匿名测试订单，连续运行真实事实探针。",
    "metric_source": "配置生产 HTTPS 指标 API、服务端鉴权和隔离租户，连续执行 3 次只读 SLA 探针。",
    "message_channel": "提供企业消息测试接收端，验证签名、重试、死信和回滚开关。",
    "production_infrastructure": "在至少两个独立目标节点完成依赖、故障切换和容量验收。",
    "real_model_holdout": "使用至少 200 条独立人工标注脱敏样本，真实模型与事实接口连续运行 3 轮。",
    "usability_test": "由至少 5 名非研发客服或主管完成无指导测试，并回测全部 P0/P1 问题。",
}

RELEASE_DECISION_LABELS = {
    "missing": "等待授权人员审批",
    "approved": "已批准",
    "rejected": "已拒绝",
    "not_verified": "审批证据无效",
    "premature_approval": "机器门禁未通过，批准无效",
}


def _release_check_detail(item: dict[str, Any]) -> str:
    detail = str(item.get("detail") or "")
    if item.get("name") == "人工发布批准" and detail.startswith("状态="):
        raw_status = detail.removeprefix("状态=")
        return f"状态={RELEASE_DECISION_LABELS.get(raw_status, raw_status)}"
    if item.get("name") != "版本可追溯":
        return detail
    return "、".join(SOURCE_BLOCKER_LABELS.get(part, part) for part in detail.split("、"))


def build_summary(report: dict[str, Any], check: dict[str, Any]) -> str:
    local = report.get("local_evidence") or {}
    normal = (local.get("preflight") or {}).get("normal") or {}
    tickets = local.get("demo_tickets") or {}
    production_shape = local.get("production_shape") or {}
    infrastructure = report.get("infrastructure_verification") or {}
    cross_host_verified = (
        infrastructure.get("cross_host_or_target_environment_verified") is True
    )
    usability = local.get("usability_test") or {}
    source_control = local.get("source_control") or {}
    policy_retrieval = local.get("policy_retrieval_benchmark") or {}
    release_decision = report.get("release_decision") or {}
    release_checks = check.get("release_checks") or []
    external_gates = report.get("external_gates", [])
    blockers = [
        str(item.get("required") or item.get("key"))
        for item in external_gates
        if item.get("status") == "blocked_external"
    ]
    if check.get("mock_mode") is True:
        runtime_mode = "本地 mock/演示"
    elif check.get("mock_mode") is False:
        runtime_mode = "真实适配器"
    else:
        runtime_mode = "运行模式未知"
    lines = [
        "# 售后工单系统本地候选版本交付摘要",
        "",
        f"> 生成时间：{datetime.now(timezone.utc).isoformat()}",
        "> 这是一份本地候选版本交接摘要，不是生产验收结论。",
        "",
        "## 当前结论",
        "",
        f"- 本地演示版本：**{'可演示' if check.get('local_demo_ready') else '不可演示'}**",
        f"- 发布候选版本：**{'可交付' if check.get('candidate_ready') else '不可交付'}**",
        f"- 生产版本：**{'已验收' if check.get('production_ready') else '未验收'}**",
        f"- 人工发布批准：**{RELEASE_DECISION_LABELS.get(str(release_decision.get('status', 'missing')), str(release_decision.get('status', 'missing')))}**",
        f"- 运行模式：**{runtime_mode}**",
        f"- 客服可用性记录：**{usability.get('status', '未知')}**",
        "",
        "## 已验证的本地证据",
        "",
        "| 项目 | 当前结果 | 说明 |",
        "|---|---|---|",
        f"| 常规 preflight | {_status(normal.get('fail') == 0)} | FAIL={normal.get('fail', '未知')} |",
        f"| 事实接口计划 | {_status((local.get('fact_probe_plan') or {}).get('status') == 'pass')} | 仅验证计划结构，未调用真实平台 |",
        f"| 演示工单 | {_status(tickets.get('status') == 'pass')} | {tickets.get('present_count', '未知')}/{tickets.get('required_count', '未知')} 条关键工单 |",
        f"| 本机 Docker 生产形态 | {_status(production_shape.get('local_production_shape_verified') is True)} | 仅代表单机演练 |",
        f"| 政策检索 Recall@5 | {_status(policy_retrieval.get('policy_retrieval_verified') is True)} | 用例={policy_retrieval.get('case_count', '未知')}，Recall@5={_display(policy_retrieval.get('recall_at_k'))} |",
        f"| 目标环境多节点 | {_status(cross_host_verified)} | 来自独立目标环境实时证据，不使用本机 Docker 字段代替 |",
        f"| 客服记录格式 | {_status(usability.get('template_valid') is True)} | 真实观察={usability.get('observed_task_count', 0)} 条，完成率={_display(usability.get('independent_completion_rate'))} |",
        "",
        "## 发布候选门禁",
        "",
        "| 项目 | 当前结果 | 说明 |",
        "|---|---|---|",
    ]
    lines.extend(
        f"| {item.get('name', '未知')} | {'通过' if item.get('status') == 'PASS' else '未通过'} | {_release_check_detail(item)} |"
        for item in release_checks
    )
    if not release_checks:
        lines.append("| 门禁证据 | 未通过 | 报告缺少 release_checks |")
    commit_sha = source_control.get("commit_sha")
    if commit_sha:
        lines.append(f"| 当前提交 | 记录 | `{str(commit_sha)[:12]}` |")
    lines.extend([
        "",
        "## 生产交付门禁",
        "",
        "| 门禁 | 当前状态 | 完成要求 |",
        "|---|---|---|",
    ])
    lines.extend(
        f"| {EXTERNAL_GATE_LABELS.get(str(item.get('key')), str(item.get('key') or '未知门禁'))} | {'已验证' if item.get('status') == 'verified_external' else '待外部验证'} | {item.get('required') or '缺少完成要求'} |"
        for item in external_gates
    )
    if not external_gates:
        lines.append("| 外部门禁清单 | 未生成 | 重新生成预发布报告 |")
    lines.extend([
        "",
        "## 当前不能宣称的内容",
        "",
    ])
    lines.extend(f"- {blocker}" for blocker in blockers)
    if not blockers:
        lines.append("- 未发现外部门禁，但仍需人工复核报告范围。")
    lines.extend(["", "## 交接后下一步", ""])
    blocked_gates = [item for item in external_gates if item.get("status") == "blocked_external"]
    for index, item in enumerate(blocked_gates, start=1):
        key = str(item.get("key") or "")
        action = EXTERNAL_GATE_ACTIONS.get(key, str(item.get("required") or "补齐该门禁证据。"))
        lines.append(f"{index}. **{EXTERNAL_GATE_LABELS.get(key, key or '未知门禁')}**：{action}")
    final_index = len(blocked_gates) + 1
    lines.append(
        f"{final_index}. 所有机器门禁通过后生成待审批模板，由授权角色核对提交、证据摘要、风险接受和回滚方案，再形成发布决定。"
    )
    lines.extend(
        [
            "",
            "## 证据文件",
            "",
            "- `docs/testing/artifacts/policy-retrieval-benchmark.json`",
            "- `docs/testing/artifacts/metric-source-result.json`（仅在真实只读探针通过后存在）",
            "- `docs/testing/artifacts/after-sales-release-readiness.json`",
            "- `docs/testing/artifacts/after-sales-delivery-check.json`",
            "- `docs/testing/artifacts/release-decision.json`（仅在授权人员完成审批后存在）",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成售后候选版本交付摘要")
    parser.add_argument("--report", default=str(REPORT))
    parser.add_argument("--check", default=str(CHECK))
    parser.add_argument("--markdown-out", required=True)
    args = parser.parse_args(argv)
    summary = build_summary(_read(Path(args.report)), _read(Path(args.check)))
    output = Path(args.markdown_out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(summary, encoding="utf-8")
    print(f"Markdown 交付摘要={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

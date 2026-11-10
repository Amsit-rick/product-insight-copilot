"""Check the local after-sales delivery candidate without overstating production readiness."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "testing" / "artifacts" / "after-sales-release-readiness.json"
PUBLIC_DEMO_EXTERNAL_GATES = frozenset(
    {
        "fact_platform_sandbox",
        "metric_source",
        "message_channel",
        "production_infrastructure",
        "real_model_holdout",
        "usability_test",
    }
)


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "detail": detail}


def build_delivery_check(report_path: Path = REPORT, max_age_hours: int = 24) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if max_age_hours <= 0:
        return {
            "schema_version": "after-sales-delivery-check-v5",
            "local_demo_ready": False,
            "candidate_ready": False,
            "production_ready": False,
            "checks": [_check("报告新鲜度参数", False, "max_age_hours 必须大于 0")],
        }
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        checks.append(_check("报告可读取", True, "JSON 格式有效"))
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(_check("报告可读取", False, type(exc).__name__))
        return {
            "schema_version": "after-sales-delivery-check-v5",
            "local_demo_ready": False,
            "candidate_ready": False,
            "production_ready": False,
            "checks": checks,
        }

    report_schema_valid = report.get("schema_version") == "after-sales-release-readiness-v5"
    checks.append(
        _check(
            "报告契约",
            report_schema_valid,
            str(report.get("schema_version") or "缺少 schema_version"),
        )
    )

    generated_at = report.get("generated_at")
    try:
        generated = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - generated.astimezone(timezone.utc)
        fresh = timedelta(0) <= age <= timedelta(hours=max_age_hours)
        checks.append(_check("报告新鲜度", fresh, f"生成于 {generated_at}，允许 {max_age_hours} 小时"))
    except (TypeError, ValueError):
        checks.append(_check("报告新鲜度", False, "generated_at 无法解析"))

    local = report.get("local_evidence") or {}
    normal = (local.get("preflight") or {}).get("normal") or {}
    checks.append(_check("常规 preflight", normal.get("fail") == 0, f"FAIL={normal.get('fail', 'unknown')}"))
    checks.append(_check("事实计划", (local.get("fact_probe_plan") or {}).get("status") == "pass", "计划结构完整"))
    checks.append(_check("演示工单", (local.get("demo_tickets") or {}).get("status") == "pass", "关键工单齐全"))
    checks.append(_check("客服记录格式", (local.get("usability_test") or {}).get("template_valid") is True, "记录结构有效"))

    environment = report.get("environment") or {}
    mock_mode = environment.get("adapter_mode") == "mock"
    usability = local.get("usability_test") or {}
    strict = (local.get("preflight") or {}).get("strict") or {}
    source_control = local.get("source_control") or {}
    production_shape = local.get("production_shape") or {}
    infrastructure = report.get("infrastructure_verification") or {}
    release_evidence = report.get("release_evidence") or {}
    release_decision = report.get("release_decision") or {}
    machine_gates_ready = release_evidence.get("machine_gates_ready") is True
    target_release_admission_verified = (
        release_evidence.get("target_release_admission_verified") is True
    )
    release_approval_verified = all(
        (
            release_decision.get("status") == "approved",
            release_decision.get("gate_status") == "verified_human",
            release_decision.get("commit_matches") is True,
            release_decision.get("evidence_matches") is True,
            release_decision.get("rollback_confirmed") is True,
        )
    )
    cross_host_verified = (
        infrastructure.get("cross_host_or_target_environment_verified") is True
    )
    external_blocked = [
        item.get("key")
        for item in report.get("external_gates", [])
        if item.get("status") == "blocked_external"
    ]
    local_checks_pass = all(item["status"] == "PASS" for item in checks)
    local_demo_ready = local_checks_pass and mock_mode

    strict_failed = strict.get("failed_checks") or []
    strict_detail = f"FAIL={strict.get('fail', 'unknown')}"
    if strict_failed:
        strict_detail += "：" + "、".join(str(item) for item in strict_failed)
    source_blockers = [str(item) for item in source_control.get("blockers") or []]
    release_checks = [
        _check("严格 preflight", strict.get("fail") == 0, strict_detail),
        _check(
            "版本可追溯",
            source_control.get("traceable") is True,
            "提交已清洁并与上游同步" if source_control.get("traceable") is True else "、".join(source_blockers) or "缺少版本证据",
        ),
        _check(
            "本机生产形态",
            production_shape.get("local_production_shape_verified") is True,
            (
                "PostgreSQL、Redis、pgvector、共享 checkpointer、双 Worker 与负载门槛已在单机 Docker 验证"
                if production_shape.get("local_production_shape_verified") is True
                else "缺少或未通过本机 Docker 生产形态证据"
            ),
        ),
        _check(
            "目标环境多节点",
            cross_host_verified,
            "目标生产环境跨主机实时检查已通过"
            if cross_host_verified
            else "缺少或未通过目标生产环境跨主机实时证据",
        ),
        _check(
            "真实事实适配器",
            not mock_mode,
            "HTTP 真实适配器" if not mock_mode else "当前仍为 mock 事实",
        ),
        _check(
            "外部门禁",
            not external_blocked,
            "无阻断" if not external_blocked else "、".join(str(item) for item in external_blocked),
        ),
        _check(
            "机器发布门禁",
            machine_gates_ready,
            "严格配置、版本、真实集成、目标环境和证据摘要均通过"
            if machine_gates_ready
            else "机器门禁尚未全部通过",
        ),
        _check(
            "统一候选准入",
            target_release_admission_verified,
            "供应链、Registry、rollout 与目标节点证据绑定同一候选版本"
            if target_release_admission_verified
            else "缺少或未通过统一候选版本准入证据",
        ),
        _check(
            "人工发布批准",
            release_approval_verified,
            (
                f"角色={release_decision.get('approver_role')}，提交和证据摘要均匹配"
                if release_approval_verified
                else f"状态={release_decision.get('status', 'missing')}"
            ),
        ),
    ]
    candidate_blockers: list[str] = []
    if strict.get("fail") != 0:
        candidate_blockers.append("strict_preflight")
    if source_control.get("traceable") is not True:
        candidate_blockers.append("version_traceability")
    if production_shape.get("local_production_shape_verified") is not True:
        candidate_blockers.append("local_production_shape")
    if not cross_host_verified:
        candidate_blockers.append("target_environment")
    if mock_mode:
        candidate_blockers.append("mock_fact_adapters")
    candidate_blockers.extend(str(item) for item in external_blocked)
    if not machine_gates_ready:
        candidate_blockers.append("machine_gates")
    if not target_release_admission_verified:
        candidate_blockers.append("target_release_admission")
    if not release_approval_verified:
        candidate_blockers.append("release_approval")

    # A candidate is locally evidenced, traceable and production-approved; mock evidence never qualifies.
    production_ready = all(
        (
            report_schema_valid,
            bool(report.get("production_ready")),
            not external_blocked,
            machine_gates_ready,
            target_release_admission_verified,
            release_approval_verified,
        )
    )
    if not candidate_blockers and not production_ready:
        candidate_blockers.append("production_decision")
    candidate_ready = local_checks_pass and not candidate_blockers and production_ready
    return {
        "schema_version": "after-sales-delivery-check-v5",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_report_generated_at": generated_at,
        "candidate_ready": candidate_ready,
        "local_demo_ready": local_demo_ready,
        "production_ready": production_ready,
        "mock_mode": mock_mode,
        "adapter_mode": environment.get("adapter_mode"),
        "local_production_shape_verified": production_shape.get(
            "local_production_shape_verified"
        ) is True,
        "cross_host_or_target_environment_verified": cross_host_verified,
        "external_blocked_gates": external_blocked,
        "candidate_blockers": candidate_blockers,
        "release_decision_status": release_decision.get("status"),
        "release_approver_role": release_decision.get("approver_role"),
        "machine_gates_ready": machine_gates_ready,
        "target_release_admission_verified": target_release_admission_verified,
        "usability_status": usability.get("status"),
        "checks": checks,
        "release_checks": release_checks,
    }


def public_demo_contract_errors(result: dict[str, Any]) -> list[str]:
    """Validate an honest public-demo evidence bundle without granting release approval."""

    errors: list[str] = []
    if result.get("schema_version") != "after-sales-delivery-check-v5":
        errors.append("delivery_check_schema_invalid")
    if result.get("mock_mode") is not True or result.get("adapter_mode") != "mock":
        errors.append("public_demo_must_use_mock_adapters")
    if result.get("candidate_ready") is not False:
        errors.append("public_demo_must_not_be_candidate_ready")
    if result.get("production_ready") is not False:
        errors.append("public_demo_must_not_be_production_ready")
    if result.get("local_production_shape_verified") is not True:
        errors.append("local_production_shape_evidence_missing")
    if result.get("cross_host_or_target_environment_verified") is not False:
        errors.append("public_demo_must_not_claim_target_environment")
    if result.get("machine_gates_ready") is not False:
        errors.append("public_demo_must_not_claim_machine_gates")
    if result.get("target_release_admission_verified") is not False:
        errors.append("public_demo_must_not_claim_target_release_admission")
    if result.get("release_decision_status") not in {None, "missing"}:
        errors.append("public_demo_must_not_include_release_approval")

    blocked = set(result.get("external_blocked_gates") or [])
    if blocked != PUBLIC_DEMO_EXTERNAL_GATES:
        errors.append("public_demo_external_gate_set_invalid")
    blockers = set(result.get("candidate_blockers") or [])
    required_blockers = PUBLIC_DEMO_EXTERNAL_GATES | {
        "target_environment",
        "mock_fact_adapters",
        "machine_gates",
        "target_release_admission",
        "release_approval",
    }
    if not required_blockers.issubset(blockers):
        errors.append("public_demo_candidate_blockers_incomplete")

    checks = {item.get("name"): item.get("status") for item in result.get("checks") or []}
    for name in ("报告可读取", "报告契约", "报告新鲜度"):
        if checks.get(name) != "PASS":
            errors.append(f"public_demo_{name}_failed")
    release_checks = {
        item.get("name"): item.get("status") for item in result.get("release_checks") or []
    }
    if release_checks.get("版本可追溯") != "PASS":
        errors.append("public_demo_version_not_traceable")
    if release_checks.get("本机生产形态") != "PASS":
        errors.append("public_demo_local_production_shape_failed")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查售后本地候选版本交付证据")
    parser.add_argument("--report", default=str(REPORT))
    parser.add_argument("--max-age-hours", type=int, default=24)
    parser.add_argument("--json-out")
    parser.add_argument("--fail-if-not-candidate", action="store_true")
    parser.add_argument("--fail-if-not-local-demo", action="store_true")
    parser.add_argument("--fail-if-public-demo-contract-invalid", action="store_true")
    args = parser.parse_args(argv)
    result = build_delivery_check(Path(args.report).resolve(), args.max_age_hours)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    print(encoded)
    if args.json_out:
        output = Path(args.json_out).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
        print(f"JSON 交付检查={output}")
    if args.fail_if_not_candidate and not result["candidate_ready"]:
        return 1
    if args.fail_if_not_local_demo and not result["local_demo_ready"]:
        return 1
    if args.fail_if_public_demo_contract_invalid:
        errors = public_demo_contract_errors(result)
        if errors:
            print("公开演示边界校验失败：" + "、".join(errors))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

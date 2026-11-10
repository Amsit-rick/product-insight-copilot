"""Validate a filled usability-test record without inventing user findings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "after-sales-usability-test-v1"
TASK_CODES = {"A", "B", "C", "D", "E"}
ROLE_NAMES = {"frontline", "supervisor"}
SENSITIVE_KEYS = {"password", "phone", "address", "customer_name", "customer_phone"}
KNOWN_PASSWORDS = {"ops123", "reviewer123", "admin123", "pm123", "eng123"}


def expected_task_matrix(participants: list[dict[str, Any]]) -> set[tuple[str, str]]:
    matrix: set[tuple[str, str]] = set()
    for participant in participants:
        participant_id = str(participant.get("participant_id") or "")
        codes = ["A", "B", "C", "D"] if participant.get("role") == "frontline" else ["A", "B", "E"]
        matrix.update((participant_id, code) for code in codes)
    return matrix


def template_payload() -> dict[str, Any]:
    participants = [
        {
            "participant_id": f"U{index}",
            "role": "frontline" if index <= 3 else "supervisor",
            "is_developer": None,
        }
        for index in range(1, 6)
    ]
    tasks = []
    for participant in participants:
        codes = ["A", "B", "C", "D"] if participant["role"] == "frontline" else ["A", "B", "E"]
        tasks.extend(
            {
                "participant_id": participant["participant_id"],
                "task_code": code,
                "completed": None,
                "independent": None,
                "duration_seconds": None,
                "mistakes": 0,
                "blocked_at": "",
                "notes": "",
            }
            for code in codes
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "test_date": "",
        "facilitator": "",
        "environment": "local_demo_only",
        "participant_scope_confirmed": None,
        "participants": participants,
        "tasks": tasks,
        "issues": [],
        "conclusion": "",
        "next_changes": [],
    }


def _walk_for_sensitive_keys(value: Any, path: str = "root") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower()
            if key_text in SENSITIVE_KEYS or "password" in key_text or "token" in key_text:
                findings.append(f"{path}.{key}")
            findings.extend(_walk_for_sensitive_keys(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(_walk_for_sensitive_keys(child, f"{path}[{index}]"))
    elif isinstance(value, str) and value in KNOWN_PASSWORDS:
        findings.append(path)
    return findings


def validate_record(payload: Any) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return None, ["顶层必须是对象"]
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version 必须是 {SCHEMA_VERSION}")
    sensitive = _walk_for_sensitive_keys(payload)
    if sensitive:
        errors.append("记录包含禁止写入的敏感字段：" + ", ".join(sensitive[:5]))

    participants = payload.get("participants")
    if not isinstance(participants, list) or len(participants) < 5:
        errors.append("participants 至少需要 5 名参与者（3 名客服、2 名主管/质检）")
        participants = participants if isinstance(participants, list) else []
    participant_ids: set[str] = set()
    role_counts = {role: 0 for role in ROLE_NAMES}
    non_developer_confirmed_count = 0
    for index, participant in enumerate(participants):
        if not isinstance(participant, dict):
            errors.append(f"participants[{index}] 必须是对象")
            continue
        participant_id = str(participant.get("participant_id") or "").strip()
        role = participant.get("role")
        if not participant_id or participant_id in participant_ids:
            errors.append(f"participants[{index}] participant_id 为空或重复")
        participant_ids.add(participant_id)
        if role not in ROLE_NAMES:
            errors.append(f"participants[{index}].role 必须是 frontline 或 supervisor")
        else:
            role_counts[role] += 1
        is_developer = participant.get("is_developer")
        if is_developer is not None and not isinstance(is_developer, bool):
            errors.append(f"participants[{index}].is_developer 必须是 true/false/null")
        if is_developer is False:
            non_developer_confirmed_count += 1
    if role_counts["frontline"] < 3 or role_counts["supervisor"] < 2:
        errors.append("参与者岗位至少需要 frontline=3、supervisor=2")

    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append("tasks 必须是非空数组")
        tasks = tasks if isinstance(tasks, list) else []
    task_count = 0
    observed_pairs: set[tuple[str, str]] = set()
    completed_count = 0
    independent_observed = 0
    independent_completed = 0
    fully_observed_task_count = 0
    issue_counts = {"P0": 0, "P1": 0, "P2": 0}
    unresolved_issue_counts = {"P0": 0, "P1": 0, "P2": 0}
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"tasks[{index}] 必须是对象")
            continue
        task_count += 1
        task_code = task.get("task_code")
        if task_code not in TASK_CODES:
            errors.append(f"tasks[{index}].task_code 必须是 A-E")
        participant_id = str(task.get("participant_id") or "")
        if participant_id not in participant_ids:
            errors.append(f"tasks[{index}] 引用了不存在的 participant_id")
        pair = (participant_id, str(task_code or ""))
        if pair in observed_pairs:
            errors.append(f"tasks[{index}] participant_id/task_code 重复")
        observed_pairs.add(pair)
        for field in ("completed", "independent"):
            if task.get(field) is not None and not isinstance(task.get(field), bool):
                errors.append(f"tasks[{index}].{field} 必须是 true/false/null")
        if task.get("completed") is True:
            completed_count += 1
        if task.get("independent") is not None:
            independent_observed += 1
            independent_completed += int(task.get("independent") is True)
        duration = task.get("duration_seconds")
        if duration is not None and (not isinstance(duration, (int, float)) or duration < 0):
            errors.append(f"tasks[{index}].duration_seconds 必须是非负数字或 null")
        if (
            isinstance(task.get("completed"), bool)
            and isinstance(task.get("independent"), bool)
            and isinstance(duration, (int, float))
            and duration >= 0
        ):
            fully_observed_task_count += 1
        mistakes = task.get("mistakes", 0)
        if not isinstance(mistakes, int) or mistakes < 0:
            errors.append(f"tasks[{index}].mistakes 必须是非负整数")
    expected_pairs = expected_task_matrix(participants)
    missing_pairs = sorted(expected_pairs - observed_pairs)
    unexpected_pairs = sorted(observed_pairs - expected_pairs)
    if missing_pairs:
        errors.append(f"缺少任务记录：{', '.join(f'{participant}/{code}' for participant, code in missing_pairs[:5])}")
    if unexpected_pairs:
        errors.append(f"存在岗位不匹配或多余任务：{', '.join(f'{participant}/{code}' for participant, code in unexpected_pairs[:5])}")
    issues = payload.get("issues")
    if issues is not None and not isinstance(issues, list):
        errors.append("issues 必须是数组")
        issues = []
    for index, issue in enumerate(issues or []):
        if not isinstance(issue, dict) or issue.get("severity") not in issue_counts:
            errors.append(f"issues[{index}].severity 必须是 P0/P1/P2")
        elif isinstance(issue, dict):
            severity = str(issue["severity"])
            issue_counts[severity] += 1
            resolved = issue.get("resolved", False)
            if not isinstance(resolved, bool):
                errors.append(f"issues[{index}].resolved 必须是 true/false")
            elif not resolved:
                unresolved_issue_counts[severity] += 1

    participant_scope_confirmed = payload.get("participant_scope_confirmed")
    if participant_scope_confirmed is not None and not isinstance(
        participant_scope_confirmed, bool
    ):
        errors.append("participant_scope_confirmed 必须是 true/false/null")

    if errors:
        return None, errors
    return {
        "schema_version": SCHEMA_VERSION,
        "record_valid": True,
        "participant_count": len(participants),
        "role_counts": role_counts,
        "task_count": task_count,
        "completed_task_count": completed_count,
        "independent_observed_count": independent_observed,
        "independent_completed_count": independent_completed,
        "fully_observed_task_count": fully_observed_task_count,
        "all_tasks_fully_observed": fully_observed_task_count == task_count,
        "independent_completion_rate": (
            round(independent_completed / independent_observed, 4) if independent_observed else None
        ),
        "issue_counts": issue_counts,
        "unresolved_issue_counts": unresolved_issue_counts,
        "non_developer_confirmed_count": non_developer_confirmed_count,
        "participant_scope_confirmed": participant_scope_confirmed is True,
        "test_metadata_complete": bool(str(payload.get("test_date") or "").strip())
        and bool(str(payload.get("facilitator") or "").strip()),
        "conclusion_provided": bool(str(payload.get("conclusion") or "").strip()),
        "note": "这是记录格式与已填写数据汇总，不代表产品达标结论。",
    }, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验客服无指导测试记录，不生成达标结论")
    parser.add_argument("--input", help="已填写的 JSON 记录")
    parser.add_argument("--template-out", help="输出空白 JSON 模板")
    parser.add_argument("--summary-out", help="输出不含原话的汇总 JSON")
    args = parser.parse_args(argv)
    if args.template_out:
        output = Path(args.template_out).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(template_payload(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"JSON 模板={output}")
    if not args.input:
        return 0
    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[FAIL] 无法读取记录：{type(exc).__name__}")
        return 2
    summary, errors = validate_record(payload)
    if errors:
        print("[FAIL] 记录格式不合格")
        for error in errors:
            print(f"- {error}")
        return 1
    assert summary is not None
    encoded = json.dumps(summary, ensure_ascii=False, indent=2)
    print(encoded)
    if args.summary_out:
        output = Path(args.summary_out).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
        print(f"JSON 汇总={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

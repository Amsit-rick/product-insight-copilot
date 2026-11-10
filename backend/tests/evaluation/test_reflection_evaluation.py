from __future__ import annotations

import json
from pathlib import Path

from app.services.reflection_guard import inspect_ticket_reply, revise_ticket_reply
from app.services.report_reflection import inspect_report, revise_report

CASES = json.loads((Path(__file__).with_name("reflection_cases.json")).read_text(encoding="utf-8"))


def test_reflection_evaluation_cases_meet_deterministic_release_gates():
    failures: list[str] = []
    revision_total = 0
    revision_passed = 0
    correct_total = 0
    false_positive = 0

    for case in CASES:
        if case["kind"] == "ticket":
            result = inspect_ticket_reply(
                reply=case["reply"],
                protected_decision=case["decision"],
                approval_status=case.get("approval_status"),
                evidence_refs=set(),
            )
            if case.get("expect_revision_pass"):
                revision_total += 1
                revision = revise_ticket_reply(
                    reply=case["reply"],
                    issues=[item.model_dump() for item in result.issues],
                    protected_decision=case["decision"],
                )
                checked = inspect_ticket_reply(
                    reply=revision["revised_text"],
                    protected_decision=case["decision"],
                    approval_status=case.get("approval_status"),
                    evidence_refs=set(),
                )
                revision_passed += checked.verdict == "pass"
        else:
            evidence_ids = set(case.get("evidence_ids") or [])
            result = inspect_report(
                report=case["report"], evidence_ids=evidence_ids, conflicts=case.get("conflicts") or []
            )
            if case.get("expect_revision_pass"):
                revision_total += 1
                revision = revise_report(
                    report=case["report"],
                    issues=[item.model_dump() for item in result.issues],
                    evidence_ids=evidence_ids,
                    conflicts=case.get("conflicts") or [],
                )
                checked = inspect_report(
                    report=revision.revised_report.model_dump(),
                    evidence_ids=evidence_ids,
                    conflicts=case.get("conflicts") or [],
                )
                revision_passed += checked.verdict == "pass"

        if result.verdict != case["expected_verdict"]:
            failures.append(f"{case['id']}: verdict={result.verdict}")
        expected_issue = case.get("expected_issue")
        if expected_issue and expected_issue not in {item.code for item in result.issues}:
            failures.append(f"{case['id']}: missing issue={expected_issue}")
        if case["expected_verdict"] == "pass":
            correct_total += 1
            false_positive += result.verdict != "pass"

    assert failures == []
    assert revision_total == 4
    assert revision_passed == revision_total
    assert false_positive == 0, f"正确草稿误修订率={false_positive}/{correct_total}"

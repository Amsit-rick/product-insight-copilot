"""诊断报告的确定性反思与结构化修订。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping

from app.schemas.reflection import ReflectionIssue, ReflectionResult, ReportRevision


def _issue(
    code: str,
    *,
    severity: str,
    category: str,
    message: str,
    auto_fixable: bool,
    evidence_refs: Iterable[str] = (),
) -> ReflectionIssue:
    return ReflectionIssue(
        code=code,
        severity=severity,
        category=category,
        target="diagnostic_report",
        source="code",
        rule_code=code,
        message=message,
        evidence_refs=list(evidence_refs),
        auto_fixable=auto_fixable,
    )


def inspect_report(
    *, report: Mapping[str, Any], evidence_ids: set[str], conflicts: list[dict]
) -> ReflectionResult:
    issues: list[ReflectionIssue] = []
    if not str(report.get("summary") or "").strip():
        issues.append(
            _issue(
                "report_summary_missing",
                severity="medium",
                category="consistency",
                message="报告缺少结论摘要",
                auto_fixable=True,
            )
        )

    for section in ("facts", "inferences"):
        for item in report.get(section) or []:
            refs = [str(ref) for ref in item.get("evidence_refs") or []]
            unknown = [ref for ref in refs if ref not in evidence_ids]
            if not refs:
                issues.append(
                    _issue(
                        "statement_missing_citation",
                        severity="medium",
                        category="citation",
                        message=f"{section} 中的陈述 {item.get('id') or ''} 缺少引用，应降级为假设",
                        auto_fixable=True,
                    )
                )
            elif unknown:
                issues.append(
                    _issue(
                        "statement_unknown_citation",
                        severity="high",
                        category="citation",
                        message=f"{section} 中的陈述引用了不存在的证据 {unknown}",
                        auto_fixable=True,
                    )
                )

    if conflicts and not (report.get("conflicts") or []):
        issues.append(
            _issue(
                "report_omits_conflicts",
                severity="high",
                category="consistency",
                message="已有证据冲突没有在报告中呈现",
                auto_fixable=True,
            )
        )

    if any(item.auto_fixable for item in issues):
        verdict = "revise"
        stop_reason = None
    elif issues:
        verdict = "human_review"
        stop_reason = "unresolved_report_issue"
    else:
        verdict = "pass"
        stop_reason = "passed"
    return ReflectionResult(
        verdict=verdict,
        issues=issues,
        required_changes=[item.message for item in issues if item.auto_fixable],
        confidence=1.0,
        stop_reason=stop_reason,
    )


def revise_report(
    *,
    report: Mapping[str, Any],
    issues: Iterable[Mapping[str, Any]],
    evidence_ids: set[str],
    conflicts: list[dict],
) -> ReportRevision:
    revised = deepcopy(dict(report))
    issue_codes = {str(item.get("code") or "") for item in issues}
    addressed: list[str] = []
    changed_sections: list[str] = []

    for section in ("facts", "inferences"):
        kept: list[dict] = []
        demoted: list[dict] = []
        for raw in revised.get(section) or []:
            item = dict(raw)
            refs = [str(ref) for ref in item.get("evidence_refs") or [] if ref in evidence_ids]
            item["evidence_refs"] = refs
            if not refs:
                item["type"] = "hypothesis"
                item.setdefault("verify_by", "补充可定位证据后再确认")
                demoted.append(item)
            else:
                kept.append(item)
        if demoted:
            revised[section] = kept
            revised.setdefault("hypotheses", []).extend(demoted)
            changed_sections.extend([section, "hypotheses"])
            for code in ("statement_missing_citation", "statement_unknown_citation"):
                if code in issue_codes and code not in addressed:
                    addressed.append(code)

    if "report_omits_conflicts" in issue_codes:
        revised["conflicts"] = deepcopy(conflicts)
        changed_sections.append("conflicts")
        addressed.append("report_omits_conflicts")

    if "report_summary_missing" in issue_codes:
        revised["summary"] = "现有证据不足以形成稳定结论，以下内容需结合引用与待验证项审阅。"
        changed_sections.append("summary")
        addressed.append("report_summary_missing")

    remaining = sorted(code for code in issue_codes if code and code not in set(addressed))
    return ReportRevision(
        revised_report=revised,
        addressed_issue_codes=addressed,
        remaining_issue_codes=remaining,
        changed_sections=list(dict.fromkeys(changed_sections)),
    )


__all__ = ["inspect_report", "revise_report"]

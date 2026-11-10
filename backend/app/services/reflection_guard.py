"""反思流程的确定性硬门禁与受保护字段快照。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from app.schemas.reflection import ReflectionIssue, ReflectionResult

PROTECTED_DECISION_FIELDS: tuple[str, ...] = (
    "issue_type",
    "action_type",
    "amount",
    "amount_source",
    "risk_tier",
    "approval_requirement",
    "policy_refs",
)

_COMPLETED_ACTION_PATTERNS = (
    r"退款已(?:完成|成功|到账|处理)",
    r"已(?:经)?(?:为您)?退款",
    r"补发已(?:完成|发出)",
    r"已(?:经)?(?:完成)?赔付",
    r"优惠券已(?:发放|到账)",
)
_PENDING_PROMISE_PATTERNS = (
    r"将为您发起退款",
    r"预计\s*\d+\s*[-至到]\s*\d+\s*个工作日到账",
    r"我们将为您(?:免费)?补发",
    r"我们会安排寄出替换商品",
)
_AMOUNT_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d{1,2})?)\s*元")
_SENSITIVE_DATA_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*\S+", re.I),
)
_INJECTION_PATTERNS = (
    "忽略安全规则",
    "忽略以上指令",
    "忽略之前的指令",
    "导出全部数据",
    "绕过权限",
    "ignore previous instructions",
    "reveal your prompt",
)


def _normalise(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalise(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _protected_values(decision: Mapping[str, Any]) -> dict[str, Any]:
    return {field: _normalise(decision.get(field)) for field in PROTECTED_DECISION_FIELDS}


@dataclass(frozen=True)
class ProtectedDecisionSnapshot:
    decision_version: int
    values: dict[str, Any]
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_version": self.decision_version,
            "values": self.values,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ProtectedComparison:
    unchanged: bool
    changed_fields: list[str]
    digest_before: str
    digest_after: str


def build_protected_snapshot(
    decision: Mapping[str, Any], *, decision_version: int
) -> ProtectedDecisionSnapshot:
    values = _protected_values(decision)
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ProtectedDecisionSnapshot(
        decision_version=max(1, int(decision_version)), values=values, digest=digest
    )


def compare_protected_snapshot(
    snapshot: ProtectedDecisionSnapshot | Mapping[str, Any], current: Mapping[str, Any]
) -> ProtectedComparison:
    if not isinstance(snapshot, ProtectedDecisionSnapshot):
        snapshot = ProtectedDecisionSnapshot(
            decision_version=int(snapshot.get("decision_version") or 1),
            values=dict(snapshot.get("values") or {}),
            digest=str(snapshot.get("digest") or ""),
        )
    current_snapshot = build_protected_snapshot(current, decision_version=snapshot.decision_version)
    changed = [
        field
        for field in PROTECTED_DECISION_FIELDS
        if snapshot.values.get(field) != current_snapshot.values.get(field)
    ]
    return ProtectedComparison(
        unchanged=not changed,
        changed_fields=changed,
        digest_before=snapshot.digest,
        digest_after=current_snapshot.digest,
    )


def reflection_idempotency_key(
    *, task_id: str, draft_kind: str, draft_version: int, reflection_round: int
) -> str:
    raw = f"{task_id}:{draft_kind}:{int(draft_version)}:{int(reflection_round)}"
    return "reflection:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _issue(
    code: str,
    *,
    severity: str,
    category: str,
    message: str,
    auto_fixable: bool = False,
    evidence_refs: Iterable[str] = (),
) -> ReflectionIssue:
    return ReflectionIssue(
        code=code,
        severity=severity,
        category=category,
        target="ticket_reply",
        source="code",
        rule_code=code,
        message=message,
        evidence_refs=list(evidence_refs),
        auto_fixable=auto_fixable,
    )


def _approved(approval_status: str | None) -> bool:
    return str(approval_status or "").lower() in {"approved", "modified_approved"}


def _amount_mismatch(reply: str, expected: Any) -> bool:
    if expected in (None, "", "0", 0):
        return False
    try:
        expected_amount = Decimal(str(expected)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return False
    amounts = []
    for raw in _AMOUNT_PATTERN.findall(reply):
        try:
            amounts.append(Decimal(raw).quantize(Decimal("0.01")))
        except InvalidOperation:
            continue
    return bool(amounts) and expected_amount not in amounts


def inspect_ticket_reply(
    *,
    reply: str,
    protected_decision: Mapping[str, Any],
    approval_status: str | None,
    evidence_refs: set[str] | None = None,
) -> ReflectionResult:
    """在任何模型 verdict 之前执行的售后回复硬门禁。"""

    issues: list[ReflectionIssue] = []
    approval_required = protected_decision.get("approval_requirement") not in (None, "", "none")
    approved = _approved(approval_status)

    if any(pattern.search(reply) for pattern in _SENSITIVE_DATA_PATTERNS):
        issues.append(
            _issue(
                "sensitive_data_exposure",
                severity="critical",
                category="sensitive_data",
                message="客户回复包含不应直接暴露的敏感信息",
            )
        )

    lowered_reply = reply.lower()
    if any(pattern.lower() in lowered_reply for pattern in _INJECTION_PATTERNS):
        issues.append(
            _issue(
                "prompt_injection_in_draft",
                severity="critical",
                category="sensitive_data",
                message="客户回复包含提示词注入或越权指令文本",
            )
        )

    if approval_required and not approved and any(
        re.search(pattern, reply) for pattern in _COMPLETED_ACTION_PATTERNS
    ):
        issues.append(
            _issue(
                "unapproved_completed_action",
                severity="critical",
                category="approval",
                message="回复声称高风险动作已经完成，但当前没有有效审批记录",
            )
        )

    if approval_required and not approved and any(
        re.search(pattern, reply) for pattern in _PENDING_PROMISE_PATTERNS
    ):
        issues.append(
            _issue(
                "approval_condition_missing",
                severity="medium",
                category="reply",
                message="回复没有明确说明动作需审批通过后才能执行",
                auto_fixable=True,
            )
        )

    if _amount_mismatch(reply, protected_decision.get("amount")):
        issues.append(
            _issue(
                "reply_amount_mismatch",
                severity="critical",
                category="amount",
                message="回复中的金额与规则决策金额不一致",
            )
        )

    if not str(reply or "").strip():
        issues.append(
            _issue(
                "empty_reply",
                severity="high",
                category="reply",
                message="客户回复为空",
                auto_fixable=False,
            )
        )

    if any(item.severity == "critical" for item in issues):
        verdict = "human_review"
        stop_reason = "critical_safety_issue"
    elif any(item.auto_fixable for item in issues):
        verdict = "revise"
        stop_reason = None
    elif issues:
        verdict = "human_review"
        stop_reason = "unresolved_hard_gate"
    else:
        verdict = "pass"
        stop_reason = "passed"

    return ReflectionResult(
        verdict=verdict,
        issues=issues,
        required_changes=[item.message for item in issues if item.auto_fixable],
        missing_evidence=[],
        confidence=1.0,
        stop_reason=stop_reason,
    )


def revise_ticket_reply(
    *, reply: str, issues: Iterable[Mapping[str, Any]], protected_decision: Mapping[str, Any]
) -> dict[str, Any]:
    """只修订客户回复文本，不接收也不返回任何受保护业务字段。"""

    issue_codes = [str(item.get("code") or "") for item in issues]
    revised = str(reply or "").strip()
    addressed: list[str] = []

    if "approval_condition_missing" in issue_codes:
        action = protected_decision.get("action_type")
        amount = protected_decision.get("amount")
        approval = protected_decision.get("approval_requirement")
        approval_label = {
            "supervisor": "主管审批",
            "risk_control": "风控审批",
            "staff_confirm": "客服确认",
        }.get(str(approval), "人工确认")
        if action == "refund":
            amount_text = f"，申请金额为 {amount} 元" if amount not in (None, "", "0") else ""
            revised = (
                f"您好，您的退款申请已记录{amount_text}。该申请需经{approval_label}通过后才能发起，"
                "审批结果和后续处理进度会及时同步给您。"
            )
        elif action in {"reship", "exchange", "return_goods", "compensate", "coupon"}:
            revised = (
                f"您好，您的售后申请已记录。该处理需经{approval_label}通过后才能执行，"
                "确认结果会及时同步给您。"
            )
        else:
            revised = re.sub(
                r"(将为您|我们将|我们会)",
                f"经{approval_label}通过后将",
                revised,
                count=1,
            )
        addressed.append("approval_condition_missing")

    remaining = [code for code in issue_codes if code and code not in addressed]
    return {
        "revised_text": revised,
        "addressed_issue_codes": addressed,
        "remaining_issue_codes": remaining,
        "changed_sections": ["customer_reply"] if revised != reply else [],
    }


__all__ = [
    "PROTECTED_DECISION_FIELDS",
    "ProtectedComparison",
    "ProtectedDecisionSnapshot",
    "build_protected_snapshot",
    "compare_protected_snapshot",
    "inspect_ticket_reply",
    "reflection_idempotency_key",
    "revise_ticket_reply",
]

"""指标 / 数据集级访问策略（设计文档 11 节：权限不交给模型）。

为什么需要单独一层：**知识库密级拦不住指标维度的越权**。
"把含营收的经营大盘报表导给我"这种请求，知识库权限天然放行（运营本来就能读 operation_kb），
真正被违反的是"机密经营指标只对特定角色开放"——这属于指标/数据集级密级。
`access_policies` 表早就建好了，但一直没接进判定链路，本模块就是把它接上。

判定语义（与 `access_policies` 表字段一一对应）：

| 字段 | 说明 |
|---|---|
| `resource_type` | `metric`（单个指标）、`metric_domain`（指标域，如 finance）、`dataset`（数据集，按指标处理） |
| `resource_id` | 资源名；支持 `*`（全部）与 `前缀*`（前缀匹配） |
| `subject_type` / `subject_id` | `role`（角色码）/ `user`（用户 id）；`*` 表示所有主体 |
| `actions` | 必须包含 `metric:read`（也接受 `read` / `*`）才参与判定 |
| `effect` | `allow` / `deny` |
| `conditions` | 目前支持 `{"min_clearance": n}`：要求主体密级不低于 n |

三条规则，按顺序求值：

1. **deny 优先**——命中任意 deny 直接拒绝。宁可少给权限，也不要因为"多了一条 allow"就漏；
2. **有策略即白名单**——该资源只要存在任意策略，就必须命中一条 allow，否则拒绝
   （这样"机密指标只对 admin/pm 开放"可以直接用两条 allow 表达，不必给每个角色写 deny）；
3. **无策略默认放行**——策略是**叠加的收紧层**，不改变既有行为（45 条已通过用例不受影响）。

拒绝结果一律带 `reason`（可读、可进报告与审计），并且**只说明"无权访问"，不泄露数值**。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_utils import get_logger
from app.models.identity import AccessPolicy
from app.services.policy_conditions import evaluate_temporal_conditions

logger = get_logger(__name__)

# 参与判定的动作名：策略里写 "metric:read" 最规范，也兼容 "read"（等价于读）与 "*"
READ_ACTIONS = {"metric:read", "metric:read_all", "read", "*"}
METRIC_RESOURCE_TYPES = ("metric", "metric_domain", "dataset")


@dataclass(frozen=True)
class AccessDecision:
    """一次访问判定的结果（拒绝时必须能说清"为什么"，否则没法向用户解释）。"""

    allowed: bool
    reason: str = ""
    resource_type: str = ""
    resource_name: str = ""
    matched_policy_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "resource_type": self.resource_type,
            "resource_name": self.resource_name,
            "matched_policy_id": self.matched_policy_id,
        }


def _resource_matches(pattern: str, name: str) -> bool:
    """资源名匹配：全匹配 / `*` 通配 / `前缀*`。"""
    pattern = (pattern or "").strip()
    if not pattern or pattern == "*":
        return pattern == "*"
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return pattern == name


def _subject_matches(policy: AccessPolicy, roles: list[str], user_id: str | None) -> bool:
    subject_type = (policy.subject_type or "role").strip().lower()
    subject_id = (policy.subject_id or "").strip()
    if subject_id in {"", "*"}:
        return True  # 对所有主体生效（通常配合 effect=deny 做广播限制）
    if subject_type == "user":
        return bool(user_id) and subject_id == user_id
    if subject_type == "role":
        return subject_id in set(roles or [])
    # 未知主体类型：按"不匹配"处理（宁可不授权，也不要误授权）
    return False


def _action_allows(policy: AccessPolicy) -> bool:
    actions = policy.actions or []
    if isinstance(actions, str):
        actions = [actions]
    return bool({str(item).strip().lower() for item in actions} & READ_ACTIONS)


def _clearance_ok(policy: AccessPolicy, clearance: int | None) -> bool:
    conditions = policy.conditions or {}
    if not isinstance(conditions, dict):
        return True
    minimum = conditions.get("min_clearance")
    if minimum is None:
        return True
    try:
        minimum_value = int(minimum)
    except (TypeError, ValueError):
        logger.warning("访问策略 min_clearance 非法，按不限制处理: %r", minimum)
        return True
    return (clearance or 0) >= minimum_value


# ---- 细粒度条件：有效期 / 时间窗 / 行级数据范围 ------------------------------

# 允许被行级条件限制的维度：白名单，避免有人往策略里写任意列名（那是注入面）
ROW_FILTER_DIMENSIONS = ("platform", "app_version", "channel", "device")


def _parse_time_of_day(value: Any) -> int | None:
    """把 "09:00" 解析成当天的分钟数；非法值返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        logger.warning("时间窗取值非法，按不限制处理: %r", value)
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        logger.warning("时间窗取值越界，按不限制处理: %r", value)
        return None
    return hour * 60 + minute


def _now_in_zone(zone: str) -> datetime:
    """按策略里声明的时区取当前时间。

    优先用固定偏移（"+08:00"，Windows 上不依赖 tzdata），其次尝试 IANA 时区名，
    都拿不到就退回 UTC 并告警——宁可按 UTC 判定（偏严或偏松都会被记录），也不要静默放行。
    """
    offset = (zone or "").strip()
    if offset:
        match = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?", offset)
        if match:
            sign = 1 if match.group(1) == "+" else -1
            hours = int(match.group(2))
            minutes = int(match.group(3) or 0)
            return datetime.now(timezone(sign * timedelta(hours=hours, minutes=minutes)))
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(offset))
        except Exception:
            logger.warning("时区 %r 不可用（Windows 可能缺 tzdata），按 UTC 判定", zone)
    return datetime.now(timezone.utc)


def _time_window_ok(policy: AccessPolicy, now: datetime) -> tuple[bool, str]:
    """时间窗条件：`{"time_window": {"start": "09:00", "end": "18:00", "days": [1,2,3,4,5], "timezone": "+08:00"}}`。

    `days` 用 ISO 星期（1=周一 … 7=周日）。支持跨零点（start > end，例如 22:00–06:00）。
    """
    window = (policy.conditions or {}).get("time_window")
    if not isinstance(window, dict) or not window:
        return True, ""
    days = window.get("days")
    if isinstance(days, list) and days:
        allowed_days = {int(day) for day in days if isinstance(day, (int, str)) and str(day).isdigit()}
        if allowed_days and now.isoweekday() not in allowed_days:
            return False, f"当前是周{'一二三四五六日'[now.isoweekday() - 1]}，不在允许的时间窗内"
    start = _parse_time_of_day(window.get("start"))
    end = _parse_time_of_day(window.get("end"))
    if start is None and end is None:
        return True, ""
    current = now.hour * 60 + now.minute
    if start is not None and end is not None:
        if start <= end:
            if not (start <= current <= end):
                return False, f"当前 {now:%H:%M} 不在允许的时间窗 {window.get('start')}–{window.get('end')} 内"
        else:  # 跨零点
            if not (current >= start or current <= end):
                return False, f"当前 {now:%H:%M} 不在允许的时间窗 {window.get('start')}–{window.get('end')} 内"
    elif start is not None and current < start:
        return False, f"当前 {now:%H:%M} 早于允许的起始时间 {window.get('start')}"
    elif end is not None and current > end:
        return False, f"当前 {now:%H:%M} 晚于允许的截止时间 {window.get('end')}"
    return True, ""


def _validity_ok(policy: AccessPolicy, now: datetime) -> tuple[bool, str]:
    """有效期条件：`{"valid_from": "2026-09-01", "valid_until": "2026-09-30"}`（含边界）。

    临时授权到点自动失效，不需要人工回来删策略——权限最容易出事的地方就是"忘了收回"。
    """
    conditions = policy.conditions or {}
    for key, label, is_start in (
        ("valid_from", "尚未生效", True),
        ("valid_until", "已过期", False),
    ):
        raw = conditions.get(key)
        if not raw:
            continue
        try:
            if isinstance(raw, str):
                boundary = datetime.fromisoformat(raw).replace(tzinfo=now.tzinfo)
            else:
                boundary = datetime.combine(raw, datetime.min.time()).replace(tzinfo=now.tzinfo)
        except (TypeError, ValueError):
            logger.warning("策略 %s 取值非法，按不限制处理: %r", key, raw)
            continue
        if is_start:
            compare = datetime.combine(boundary.date(), datetime.min.time()).replace(tzinfo=now.tzinfo)
            if now < compare:
                return False, f"该授权 {raw} 才生效（{label}）"
        else:
            # 到期日当天仍然有效：按当天 23:59:59 计
            end_of_day = datetime.combine(boundary.date(), datetime.max.time()).replace(tzinfo=now.tzinfo)
            if now > end_of_day:
                return False, f"该授权已于 {raw} 到期（{label}）"
    return True, ""


def _row_filters_of(policy: AccessPolicy) -> dict[str, list[str]]:
    """行级数据范围条件：`{"row_filters": {"channel": ["channel_a"]}}`。

    只允许白名单维度；值统一转成字符串列表。非法维度会被丢弃并告警（而不是直接放行整表）。
    """
    raw = (policy.conditions or {}).get("row_filters")
    if not isinstance(raw, dict) or not raw:
        return {}
    filters: dict[str, list[str]] = {}
    for dimension, values in raw.items():
        if dimension not in ROW_FILTER_DIMENSIONS:
            logger.warning("策略里的行级维度 %r 不在白名单 %s，已忽略", dimension, ROW_FILTER_DIMENSIONS)
            continue
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        cleaned = [str(item) for item in values if str(item).strip()]
        if cleaned:
            filters[dimension] = cleaned
    return filters


def merge_row_filters(policies: list[AccessPolicy]) -> dict[str, list[str]]:
    """合并多条 allow 策略的行级范围：allow 是**并集**授权，"任一条不限"即该维度不限。

    例：策略 A 限 channel_a、策略 B 不限渠道 → 合并不限渠道（B 已经授予了全部渠道）；
        策略 A 限 channel_a、策略 B 限 channel_b → 允许两者。
    这条语义要在文档和测试里都说清楚，否则很容易被理解成"取交集"（那会无谓地把人卡住）。
    """
    return merge_filter_maps([_row_filters_of(policy) for policy in policies])


def merge_filter_maps(maps: list[dict[str, list[str]]]) -> dict[str, list[str]]:
    """同上，但输入已经是"行级范围字典"（权限预演手里只有字典，没有策略行）。"""
    restricted: dict[str, set[str] | None] = {}
    for filters in maps:
        for dimension in ROW_FILTER_DIMENSIONS:
            values = (filters or {}).get(dimension)
            if dimension not in restricted:
                restricted[dimension] = set(values) if values else None
            elif not values:
                restricted[dimension] = None  # 有一条不限制 → 该维度整体不限制
            elif restricted[dimension] is not None:
                restricted[dimension] |= set(values)
    return {
        dimension: sorted(values)
        for dimension, values in restricted.items()
        if values
    }


def _policies_for(session: Session, tenant_id: str, resource_name: str, resource_type: str) -> list[AccessPolicy]:
    """取出可能命中该资源的**读**策略行（同租户 + 通配租户 `*`）。

    资源匹配在 Python 侧做（表极小、且要支持 `前缀*`），数据库侧只做租户与类型过滤。
    只保留"读"相关的策略：`actions=["metric:write"]` 的规则不该把读权限一起锁死，
    也不能因为它的存在就触发白名单语义（否则限制写权限会顺手禁掉读）。
    """
    rows = (
        session.query(AccessPolicy)
        .filter(AccessPolicy.tenant_id.in_([tenant_id, "*"]))
        .filter(AccessPolicy.resource_type.in_(METRIC_RESOURCE_TYPES))
        .all()
    )
    return [
        row
        for row in rows
        if (row.resource_type or "metric").lower() == resource_type
        and _resource_matches(row.resource_id, resource_name)
        and _action_allows(row)
    ]


# ---- 逐条策略的判定结论（真实判定与"权限预演"共用同一份实现）-------------------
#
# 为什么抽出来：权限预演要能回答"某个账号为什么看不到某个指标"。如果预演另写一套判定，
# 它和真实判定迟早会分叉 —— 那时候预演就是在骗人，比没有预演更糟。
# 所以这里只保留**一份**判定，`evaluate_access` / `resolve_metric_scope` / `explain_access`
# 全部建立在它之上。

# 机器可读的结论码：界面按它着色、测试按它断言（比匹配中文串稳）
VERDICT_ALLOWED = "allowed"
VERDICT_SUBJECT_MISMATCH = "subject_mismatch"
VERDICT_CLEARANCE = "clearance_insufficient"
VERDICT_EXPLICIT_DENY = "explicit_deny"
VERDICT_CONDITION_UNMET = "condition_unmet"

# 命中任意一个就直接拒绝（deny 优先）
_DENY_CODES = (VERDICT_CLEARANCE, VERDICT_EXPLICIT_DENY)

VERDICT_LABELS = {
    VERDICT_ALLOWED: "已授权",
    VERDICT_SUBJECT_MISMATCH: "主体不匹配",
    VERDICT_CLEARANCE: "密级不足",
    VERDICT_EXPLICIT_DENY: "显式拒绝",
    VERDICT_CONDITION_UNMET: "条件未满足",
}


@dataclass(frozen=True)
class PolicyVerdict:
    """单条策略对某个主体的判定结论（权限预演逐条展示的就是它）。"""

    policy_id: str
    resource_type: str
    resource_id: str
    subject: str
    effect: str
    code: str
    detail: str = ""
    row_filters: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return VERDICT_LABELS.get(self.code, self.code)

    def as_dict(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "subject": self.subject,
            "effect": self.effect,
            "code": self.code,
            "label": self.label,
            "detail": self.detail,
            "row_filters": dict(self.row_filters or {}),
        }


def _subject_label(policy: AccessPolicy) -> str:
    subject_type = (policy.subject_type or "*").strip() or "*"
    subject_id = (policy.subject_id or "*").strip() or "*"
    if subject_type == "*" and subject_id == "*":
        return "*"
    return f"{subject_type}:{subject_id}"


def classify_policies(
    session: Session,
    *,
    tenant_id: str,
    roles: list[str] | None,
    user_id: str | None = None,
    clearance: int | None = None,
    resource_name: str,
    resource_type: str = "metric",
) -> list[PolicyVerdict]:
    """把"可能命中该资源的读策略"逐条判定成结论。

    **判定顺序与原来的 `evaluate_access` 严格一致**（主体 → 密级 → 显式拒绝 → 细粒度条件），
    这样重构才不会改变任何既有行为。
    """
    verdicts: list[PolicyVerdict] = []
    for policy in _policies_for(session, tenant_id, resource_name, resource_type):
        effect = (policy.effect or "allow").lower()
        # 行级范围对所有结论都带上：预演要能看出"这条策略本来会限制什么"
        verdict = PolicyVerdict(
            policy_id=policy.id,
            resource_type=policy.resource_type,
            resource_id=policy.resource_id,
            subject=_subject_label(policy),
            effect=effect,
            code=VERDICT_ALLOWED,
            row_filters=_row_filters_of(policy),
        )
        if not _subject_matches(policy, list(roles or []), user_id):
            verdicts.append(
                replace(verdict, code=VERDICT_SUBJECT_MISMATCH, detail="该策略不适用于当前角色/账号")
            )
            continue
        if not _clearance_ok(policy, clearance):
            verdicts.append(replace(verdict, code=VERDICT_CLEARANCE, detail="密级不足"))
            continue
        if effect == "deny":
            verdicts.append(replace(verdict, code=VERDICT_EXPLICIT_DENY, detail="命中显式拒绝策略"))
            continue
        temporal_ok, temporal_reason = evaluate_temporal_conditions(policy.conditions or {})
        if not temporal_ok:
            verdicts.append(
                replace(
                    verdict,
                    code=VERDICT_CONDITION_UNMET,
                    detail=temporal_reason,
                )
            )
            continue
        verdicts.append(verdict)
    return verdicts


def _decide_from_verdicts(
    verdicts: list[PolicyVerdict], resource_type: str, resource_name: str
) -> AccessDecision:
    """三条规则的聚合：deny 优先 → 有策略即白名单 → 无策略默认放行。"""
    if not verdicts:
        # 该资源没有任何读策略：策略是"叠加的收紧层"，不改变既有行为
        return AccessDecision(True, "", resource_type, resource_name)
    denies = sorted({item.detail for item in verdicts if item.code in _DENY_CODES})
    conditional = sorted({item.detail for item in verdicts if item.code == VERDICT_CONDITION_UNMET})
    allows = [item for item in verdicts if item.code == VERDICT_ALLOWED]
    if denies:
        return AccessDecision(
            False,
            f"{resource_type}「{resource_name}」的访问被策略拒绝（{'；'.join(denies)}）",
            resource_type,
            resource_name,
        )
    if allows:
        return AccessDecision(
            True, "", resource_type, resource_name, matched_policy_id=allows[0].policy_id
        )
    if conditional:
        # 有授权但当前不可用（过期 / 不在时间窗）：说清楚原因，便于用户判断"什么时候能用"
        return AccessDecision(
            False,
            f"{resource_type}「{resource_name}」的授权当前不可用：{'；'.join(conditional)}",
            resource_type,
            resource_name,
        )
    # 该资源有策略但没有一条允许当前主体 → 白名单拒绝
    return AccessDecision(
        False,
        f"{resource_type}「{resource_name}」未对当前角色开放（可申请指标授权后重试）",
        resource_type,
        resource_name,
    )


def evaluate_access(
    session: Session,
    *,
    tenant_id: str,
    roles: list[str] | None,
    user_id: str | None = None,
    clearance: int | None = None,
    resource_type: str = "metric",
    resource_name: str,
) -> AccessDecision:
    """判定某主体能否读取某个指标 / 指标域。

    返回的 `reason` 可以直接进报告、审计与接口错误体（不含任何数值）。
    """
    verdicts = classify_policies(
        session,
        tenant_id=tenant_id,
        roles=roles,
        user_id=user_id,
        clearance=clearance,
        resource_name=resource_name,
        resource_type=resource_type,
    )
    return _decide_from_verdicts(verdicts, resource_type, resource_name)


def can_read_metric(
    session: Session,
    *,
    tenant_id: str,
    roles: list[str] | None,
    user_id: str | None = None,
    clearance: int | None = None,
    metric_name: str,
) -> AccessDecision:
    return evaluate_access(
        session,
        tenant_id=tenant_id,
        roles=roles,
        user_id=user_id,
        clearance=clearance,
        resource_type="metric",
        resource_name=metric_name,
    )


def can_read_domain(
    session: Session,
    *,
    tenant_id: str,
    roles: list[str] | None,
    user_id: str | None = None,
    clearance: int | None = None,
    domain: str,
) -> AccessDecision:
    return evaluate_access(
        session,
        tenant_id=tenant_id,
        roles=roles,
        user_id=user_id,
        clearance=clearance,
        resource_type="metric_domain",
        resource_name=domain,
    )


def domains_in_query(query: str) -> list[str]:
    """从问题文本里识别**受限指标域**（关键词表来自配置，可按租户自行扩充）。

    为什么要靠关键词而不是让模型判断：这是一道权限闸门，必须可解释、可测试、可复现。
    模型只负责"取数范围"，不参与权限裁决（设计文档 7.3 节）。
    """
    text = query or ""
    domains: list[str] = []
    for domain, keywords in (get_settings().restricted_metric_domains or {}).items():
        if any(str(word) and str(word) in text for word in (keywords or [])):
            domains.append(domain)
    return sorted(set(domains))


def domain_denials(
    session: Session,
    *,
    tenant_id: str,
    roles: list[str] | None,
    user_id: str | None = None,
    clearance: int | None = None,
    query: str,
) -> list[AccessDecision]:
    """问题文本命中受限指标域时，逐域判定并返回被拒的域。"""
    denied: list[AccessDecision] = []
    for domain in domains_in_query(query):
        decision = can_read_domain(
            session,
            tenant_id=tenant_id,
            roles=roles,
            user_id=user_id,
            clearance=clearance,
            domain=domain,
        )
        if not decision.allowed:
            denied.append(decision)
    return denied


@dataclass(frozen=True)
class MetricScope:
    """一次指标访问的**有效范围**：能不能读，以及能读到哪些行。

    为什么要把"能不能读"和"能读哪些行"放在一起返回：
    只判"能不能读"会让"某角色只能看本渠道数据"这类要求无处落地，
    而这种要求在企业里非常常见（按部门/渠道/机型划分数据范围）。
    """

    allowed: bool
    reason: str = ""
    # 维度 -> 允许值；空表示该维度不限制
    row_filters: dict[str, list[str]] = field(default_factory=dict)
    matched_policy_id: str | None = None

    @property
    def filtered(self) -> bool:
        return bool(self.row_filters)

    @property
    def note(self) -> str:
        """给用户/报告看的说明（不泄露任何数值）。"""
        if not self.row_filters:
            return ""
        parts = "；".join(
            f"{dimension} ∈ [{', '.join(values)}]" for dimension, values in sorted(self.row_filters.items())
        )
        return f"已按数据范围策略过滤：{parts}"

    @property
    def cache_signature(self) -> str:
        """范围指纹：进缓存键。

        策略改动（例如把 channel_a 放宽到 channel_a+channel_b）必须让旧缓存失效，
        否则会把"原本无权看到的数据"从缓存里发出去——这是权限系统里最隐蔽的漏法。
        """
        raw = "|".join(
            f"{dimension}={','.join(sorted(values))}" for dimension, values in sorted(self.row_filters.items())
        )
        if not raw:
            return "all"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def resolve_metric_scope(
    session: Session,
    *,
    tenant_id: str,
    roles: list[str] | None,
    user_id: str | None = None,
    clearance: int | None = None,
    metric_name: str,
) -> MetricScope:
    """指标访问的完整判定：是否允许 + 行级数据范围。"""
    verdicts = classify_policies(
        session,
        tenant_id=tenant_id,
        roles=roles,
        user_id=user_id,
        clearance=clearance,
        resource_name=metric_name,
        resource_type="metric",
    )
    decision = _decide_from_verdicts(verdicts, "metric", metric_name)
    if not decision.allowed:
        return MetricScope(False, decision.reason)
    # 只合并"对当前主体有效"的 allow 策略（即 code=allowed 的那些）：
    # 否则过期策略上的限制会顺带缩窄有效策略的范围。
    return MetricScope(
        True,
        "",
        merge_filter_maps([item.row_filters for item in verdicts if item.code == VERDICT_ALLOWED]),
        decision.matched_policy_id,
    )


@dataclass(frozen=True)
class AccessExplanation:
    """一次判定的**完整过程**：结论 + 逐条策略判定 + 生效的行级范围。

    权限预演（界面「密级策略」页）就用它回答"为什么看不到这个指标"。
    """

    decision: AccessDecision
    verdicts: list[PolicyVerdict]
    effective_row_filters: dict[str, list[str]]

    @property
    def has_policies(self) -> bool:
        """该资源是否存在读策略。false 时按"无策略默认放行"处理，界面要说清楚。"""
        return bool(self.verdicts)

    @property
    def allowed_policy_ids(self) -> list[str]:
        return [item.policy_id for item in self.verdicts if item.code == VERDICT_ALLOWED]

    def as_dict(self) -> dict:
        return {
            "allowed": self.decision.allowed,
            "reason": self.decision.reason,
            "resource_type": self.decision.resource_type,
            "resource_name": self.decision.resource_name,
            "matched_policy_id": self.decision.matched_policy_id,
            "has_policies": self.has_policies,
            "allowed_policy_ids": self.allowed_policy_ids,
            "effective_row_filters": dict(self.effective_row_filters),
            "verdicts": [item.as_dict() for item in self.verdicts],
        }


def explain_access(
    session: Session,
    *,
    tenant_id: str,
    roles: list[str] | None,
    user_id: str | None = None,
    clearance: int | None = None,
    resource_name: str,
    resource_type: str = "metric",
) -> AccessExplanation:
    """与 `evaluate_access` 同源，但把判定过程一并返回（供权限预演逐条展示）。"""
    verdicts = classify_policies(
        session,
        tenant_id=tenant_id,
        roles=roles,
        user_id=user_id,
        clearance=clearance,
        resource_name=resource_name,
        resource_type=resource_type,
    )
    return AccessExplanation(
        decision=_decide_from_verdicts(verdicts, resource_type, resource_name),
        verdicts=verdicts,
        effective_row_filters=merge_filter_maps(
            [item.row_filters for item in verdicts if item.code == VERDICT_ALLOWED]
        ),
    )


def check_dimension_values(
    scope: MetricScope, requested: dict[str, str | None]
) -> str | None:
    """请求里显式指定的维度值是否都在数据范围内；返回违规说明或 None。

    为什么要拦：如果只把行级过滤加进 SQL，用户请求 `channel=channel_b` 时会拿到**空结果**，
    看起来像"这个渠道没数据"——而真实原因是"你无权看这个渠道"。权限问题不能被伪装成数据缺失。
    """
    if not scope.filtered:
        return None
    for dimension, values in scope.row_filters.items():
        wanted = requested.get(dimension)
        if wanted and wanted not in values:
            return f"请求的 {dimension}={wanted} 不在你的数据范围（{dimension} ∈ [{', '.join(values)}]）内"
    return None


def visible_metrics(    session: Session,
    *,
    tenant_id: str,
    roles: list[str] | None,
    user_id: str | None = None,
    clearance: int | None = None,
    metric_names: list[str],
) -> list[str]:
    """从候选指标里筛出当前主体可读的那些（用于计划与指标清单，避免"先规划后拒绝"）。"""
    result: list[str] = []
    for name in metric_names:
        decision = can_read_metric(
            session,
            tenant_id=tenant_id,
            roles=roles,
            user_id=user_id,
            clearance=clearance,
            metric_name=name,
        )
        if decision.allowed:
            result.append(name)
        else:
            logger.info("指标 %s 对角色 %s 不可见：%s", name, roles, decision.reason)
    return result


__all__ = [
    "AccessDecision",
    "AccessExplanation",
    "MetricScope",
    "PolicyVerdict",
    "ROW_FILTER_DIMENSIONS",
    "VERDICT_ALLOWED",
    "VERDICT_CLEARANCE",
    "VERDICT_CONDITION_UNMET",
    "VERDICT_EXPLICIT_DENY",
    "VERDICT_SUBJECT_MISMATCH",
    "can_read_domain",
    "can_read_metric",
    "check_dimension_values",
    "classify_policies",
    "domain_denials",
    "domains_in_query",
    "evaluate_access",
    "explain_access",
    "merge_filter_maps",
    "merge_row_filters",
    "resolve_metric_scope",
    "visible_metrics",
]

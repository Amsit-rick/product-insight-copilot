"""权限预演（`classify_policies` / `explain_access`）的单元测试。

这一层的核心风险不是"预演能不能算"，而是**预演和真实判定会不会分叉**。
一旦分叉，管理员看到的解释就是错的 —— 那比没有预演更危险。
所以这里除了逐个分支，还专门有一条"对同一批场景逐个断言
`explain_access` 的结论与 `evaluate_access` 完全一致"的对照测试。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.identity import AccessPolicy
from app.services.metric_access import (
    VERDICT_ALLOWED,
    VERDICT_CLEARANCE,
    VERDICT_CONDITION_UNMET,
    VERDICT_EXPLICIT_DENY,
    VERDICT_SUBJECT_MISMATCH,
    evaluate_access,
    explain_access,
    merge_filter_maps,
    merge_row_filters,
    resolve_metric_scope,
)

TENANT = "t-explain"
METRIC = "order_conversion_rate"


def add_policy(
    session: Session,
    *,
    subject_id: str,
    subject_type: str = "role",
    resource_type: str = "metric",
    resource_id: str = METRIC,
    effect: str = "allow",
    actions: list[str] | None = None,
    conditions: dict | None = None,
) -> AccessPolicy:
    row = AccessPolicy(
        tenant_id=TENANT,
        subject_type=subject_type,
        subject_id=subject_id,
        resource_type=resource_type,
        resource_id=resource_id,
        effect=effect,
        actions=actions if actions is not None else ["metric:read"],
        conditions=conditions or {},
    )
    session.add(row)
    session.flush()
    return row


def explain(session: Session, roles: list[str], *, clearance: int = 3):
    return explain_access(
        session, tenant_id=TENANT, roles=roles, clearance=clearance, resource_name=METRIC
    )


def codes(explanation) -> list[str]:
    return [item.code for item in explanation.verdicts]


# --------------------------------------------------------------------- 默认放行
def test_没有策略时放行且说明没有策略(session: Session):
    result = explain(session, ["product_manager"])
    assert result.decision.allowed is True
    assert result.has_policies is False
    assert result.verdicts == []
    assert result.effective_row_filters == {}


# --------------------------------------------------------------------- 四种结论
def test_命中允许策略(session: Session):
    policy = add_policy(session, subject_id="product_manager")
    result = explain(session, ["product_manager"])
    assert result.decision.allowed is True
    assert result.decision.matched_policy_id == policy.id
    assert codes(result) == [VERDICT_ALLOWED]
    assert result.allowed_policy_ids == [policy.id]


def test_主体不匹配时白名单拒绝(session: Session):
    """有策略但没给当前角色 → 拒绝，并且要能看出"这条策略是给别人的"。"""
    add_policy(session, subject_id="admin")
    result = explain(session, ["engineer"])
    assert result.decision.allowed is False
    assert codes(result) == [VERDICT_SUBJECT_MISMATCH]
    assert "未对当前角色开放" in result.decision.reason


def test_显式拒绝策略(session: Session):
    add_policy(session, subject_id="engineer", effect="deny")
    result = explain(session, ["engineer"])
    assert result.decision.allowed is False
    assert codes(result) == [VERDICT_EXPLICIT_DENY]
    assert "命中显式拒绝策略" in result.decision.reason


def test_密级不足(session: Session):
    add_policy(session, subject_id="engineer", conditions={"min_clearance": 3})
    result = explain(session, ["engineer"], clearance=1)
    assert result.decision.allowed is False
    assert codes(result) == [VERDICT_CLEARANCE]
    assert "密级不足" in result.decision.reason
    # 同一个角色、密级够了就应该放行（否则是别的分支在拦）
    assert explain(session, ["engineer"], clearance=3).decision.allowed is True


def test_有效期已过视为条件未满足(session: Session):
    add_policy(session, subject_id="engineer", conditions={"valid_until": "2020-01-01"})
    result = explain(session, ["engineer"])
    assert result.decision.allowed is False
    assert codes(result) == [VERDICT_CONDITION_UNMET]
    # 条件未满足的文案要指出"什么时候能用"，而不是笼统说无权
    assert "过期" in result.decision.reason
    assert "授权当前不可用" in result.decision.reason


def test_时间窗之外视为条件未满足(session: Session):
    # 只允许周一的 00:00–00:01，几乎必然不在窗内（跨零点写法等价于这一分钟）
    add_policy(session, subject_id="engineer", conditions={"time_window": {"days": [1], "start": "00:00", "end": "00:01"}})
    result = explain(session, ["engineer"])
    # 周一的那一分钟内会放行，因此断言"要么放行、要么给出时间窗原因"，避免用当前时钟做脆弱断言
    if not result.decision.allowed:
        assert codes(result) == [VERDICT_CONDITION_UNMET]
        assert "时间窗" in result.decision.reason or "不在允许" in result.decision.reason


# --------------------------------------------------------------------- 规则边界
def test_deny_优先于_allow(session: Session):
    """同时命中 allow 与 deny 必须拒绝 —— 预演也要给出两条结论，而不是只报一条。"""
    add_policy(session, subject_id="*")
    add_policy(session, subject_id="engineer", effect="deny")
    result = explain(session, ["engineer"])
    assert result.decision.allowed is False
    assert sorted(codes(result)) == sorted([VERDICT_ALLOWED, VERDICT_EXPLICIT_DENY])


def test_多角色命中任一条允许即放行(session: Session):
    add_policy(session, subject_id="admin")
    add_policy(session, subject_id="product_manager")
    result = explain(session, ["product_manager", "reviewer"])
    assert result.decision.allowed is True
    assert codes(result).count(VERDICT_ALLOWED) == 1
    assert codes(result).count(VERDICT_SUBJECT_MISMATCH) == 1


def test_过期策略不会缩窄有效策略的行级范围(session: Session):
    """一条已过期的限制策略，不应该把仍然有效的授权顺带卡窄。"""
    add_policy(session, subject_id="engineer", conditions={"row_filters": {"channel": ["channel_a"]}, "valid_until": "2020-01-01"})
    add_policy(session, subject_id="engineer")
    result = explain(session, ["engineer"])
    assert result.decision.allowed is True
    assert result.effective_row_filters == {}  # 有效的那条不限制 → 整体不限制


# --------------------------------------------------------------------- 与真实判定一致
def test_预演的行级范围与真实请求一致(session: Session):
    """预演算出的行级范围必须与真正查询时用的 `MetricScope` 完全相同。"""
    add_policy(session, subject_id="engineer", conditions={"row_filters": {"channel": ["channel_a"]}})
    add_policy(session, subject_id="reviewer", conditions={"row_filters": {"channel": ["channel_b"]}})
    explanation = explain(session, ["engineer"])
    scope = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["engineer"], clearance=3, metric_name=METRIC
    )
    assert explanation.decision.allowed is True
    assert scope.allowed is True
    assert explanation.effective_row_filters == scope.row_filters == {"channel": ["channel_a"]}
    assert explanation.decision.matched_policy_id == scope.matched_policy_id


@pytest.mark.parametrize(
    "policies,roles,clearance",
    [
        ([], ["engineer"], 1),
        ([{"subject_id": "engineer"}], ["engineer"], 1),
        ([{"subject_id": "admin"}], ["engineer"], 1),
        ([{"subject_id": "engineer", "effect": "deny"}], ["engineer"], 1),
        ([{"subject_id": "*"}], ["engineer"], 1),
        ([{"subject_id": "engineer", "conditions": {"min_clearance": 3}}], ["engineer"], 1),
        ([{"subject_id": "engineer", "conditions": {"min_clearance": 3}}], ["engineer"], 3),
        ([{"subject_id": "engineer", "conditions": {"valid_until": "2020-01-01"}}], ["engineer"], 3),
        ([{"subject_id": "engineer", "conditions": {"valid_from": "2099-01-01"}}], ["engineer"], 3),
        ([{"subject_id": "*"}, {"subject_id": "engineer", "effect": "deny"}], ["engineer"], 1),
        ([{"subject_id": "admin"}, {"subject_id": "engineer"}], ["engineer"], 1),
        ([{"resource_id": "other_metric", "subject_id": "engineer"}], ["engineer"], 1),
    ],
)
def test_预演结论与真实判定永远一致(session: Session, policies, roles, clearance):
    """核心保障：预演不能是"另一套近似算法"。

    同一批场景分别跑 `explain_access`（界面看的）与 `evaluate_access`（请求真正走的），
    结论必须逐条相同 —— 这条测试就是防止两边日后各自演化。
    """
    for spec in policies:
        add_policy(session, **spec)
    explanation = explain_access(
        session, tenant_id=TENANT, roles=roles, clearance=clearance, resource_name=METRIC
    )
    decision = evaluate_access(
        session, tenant_id=TENANT, roles=roles, clearance=clearance, resource_name=METRIC
    )
    assert explanation.decision.allowed == decision.allowed
    assert explanation.decision.reason == decision.reason
    assert explanation.decision.matched_policy_id == decision.matched_policy_id


# --------------------------------------------------------------------- 行级范围合并
def test_merge_filter_maps_与_merge_row_filters_等价(session: Session):
    """预演手里只有字典、真实判定手里是策略行：两者合并结果必须一致（并集语义）。"""
    first = add_policy(session, subject_id="a", conditions={"row_filters": {"channel": ["channel_a"]}})
    second = add_policy(session, subject_id="b", conditions={"row_filters": {"channel": ["channel_b"], "device": ["high_end"]}})
    from app.services.metric_access import _row_filters_of  # noqa: PLC0415

    assert merge_row_filters([first, second]) == merge_filter_maps(
        [_row_filters_of(first), _row_filters_of(second)]
    )


def test_一条不限制则整体不限制(session: Session):
    assert merge_filter_maps([{"channel": ["channel_a"]}, {}]) == {}
    assert merge_filter_maps([{"channel": ["channel_a"]}, {"channel": ["channel_b"]}]) == {
        "channel": ["channel_a", "channel_b"]
    }

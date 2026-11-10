"""指标 / 数据集级密级的单元测试。

这块最容易出错的不是"能不能拦"，而是三条边界：

1. **deny 优先**：同时命中 allow 与 deny 时必须拒绝（否则多配一条 allow 就漏了）；
2. **有策略即白名单**：某个指标只要有人配了策略，没被明确允许的角色就应当被拒绝
   （否则"机密指标只对几个人开放"没法表达，只能给每个角色写 deny）；
3. **无策略默认放行**：策略是叠加的收紧层，不能把既有行为一刀切掉
   （否则接上策略当天，所有指标查询全 403）。
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy.orm import Session

from app.models.identity import AccessPolicy
from app.services.metric_access import (
    can_read_domain,
    can_read_metric,
    domain_denials,
    domains_in_query,
    evaluate_access,
    visible_metrics,
)

TENANT = "t-policy"


def add_policy(
    session: Session,
    *,
    subject_id: str,
    subject_type: str = "role",
    resource_type: str = "metric",
    resource_id: str,
    effect: str = "allow",
    actions: list[str] | None = None,
    conditions: dict | None = None,
    tenant_id: str = TENANT,
) -> AccessPolicy:
    row = AccessPolicy(
        tenant_id=tenant_id,
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


# --------------------------------------------------------------------- 默认放行
def test_没有策略时默认放行(session):
    decision = can_read_metric(session, tenant_id=TENANT, roles=["engineer"], metric_name="dau")

    assert decision.allowed is True
    assert decision.reason == ""


def test_其它租户的策略不影响本租户(session):
    add_policy(session, subject_id="engineer", resource_id="gmv", effect="deny", tenant_id="other-tenant")

    assert can_read_metric(session, tenant_id=TENANT, roles=["engineer"], metric_name="gmv").allowed


# ------------------------------------------------------------------- 白名单语义
def test_有策略即白名单未授权角色被拒(session):
    add_policy(session, subject_id="finance", resource_id="gmv")

    allowed = can_read_metric(session, tenant_id=TENANT, roles=["finance"], metric_name="gmv")
    denied = can_read_metric(session, tenant_id=TENANT, roles=["engineer"], metric_name="gmv")

    assert allowed.allowed is True
    assert allowed.matched_policy_id
    assert denied.allowed is False
    assert "未对当前角色开放" in denied.reason


def test_不同指标互不影响(session):
    add_policy(session, subject_id="finance", resource_id="gmv")

    assert can_read_metric(session, tenant_id=TENANT, roles=["engineer"], metric_name="dau").allowed


# --------------------------------------------------------------------- deny 优先
def test_deny_优先于_allow(session):
    add_policy(session, subject_id="engineer", resource_id="gmv", effect="deny")
    add_policy(session, subject_id="engineer", resource_id="gmv", effect="allow")

    decision = can_read_metric(session, tenant_id=TENANT, roles=["engineer"], metric_name="gmv")

    assert decision.allowed is False
    assert "拒绝" in decision.reason


def test_广播_deny_可以覆盖所有人(session):
    add_policy(session, subject_id="*", resource_id="finance_raw_ledger", effect="deny")

    for roles in (["admin"], ["finance"], ["engineer"]):
        assert not can_read_metric(
            session, tenant_id=TENANT, roles=roles, metric_name="finance_raw_ledger"
        ).allowed


def test_不相关动作的_deny_不生效(session):
    """策略动作是 metric:write 时不参与"读"的判定——不然写权限限制会顺手把读也禁掉。"""
    add_policy(
        session,
        subject_id="engineer",
        resource_id="gmv",
        effect="deny",
        actions=["metric:write"],
    )

    assert can_read_metric(session, tenant_id=TENANT, roles=["engineer"], metric_name="gmv").allowed


def test_actions_为空不参与判定(session):
    add_policy(session, subject_id="engineer", resource_id="gmv", effect="deny", actions=[])

    assert can_read_metric(session, tenant_id=TENANT, roles=["engineer"], metric_name="gmv").allowed


# ------------------------------------------------------------------ 资源名匹配
@pytest.mark.parametrize(
    ("pattern", "name", "matched"),
    [
        ("gmv", "gmv", True),
        ("gmv", "gmvc", False),
        ("*", "anything", True),
        ("finance_*", "finance_gmv", True),
        ("finance_*", "operation_gmv", False),
    ],
)
def test_资源名支持通配与前缀(session, pattern, name, matched):
    """用 deny 策略来验证匹配范围：命中即拒绝，不命中则按"无策略"默认放行。"""
    add_policy(session, subject_id="analyst", resource_id=pattern, effect="deny")

    decision = can_read_metric(session, tenant_id=TENANT, roles=["analyst"], metric_name=name)

    assert decision.allowed is (not matched)


# ------------------------------------------------------------------ 主体与条件
def test_按用户授权的策略只对本人生效(session):
    add_policy(session, subject_type="user", subject_id="u-1", resource_id="gmv")

    assert can_read_metric(
        session, tenant_id=TENANT, roles=["engineer"], user_id="u-1", metric_name="gmv"
    ).allowed
    assert not can_read_metric(
        session, tenant_id=TENANT, roles=["engineer"], user_id="u-2", metric_name="gmv"
    ).allowed


def test_密级条件不足时拒绝(session):
    add_policy(session, subject_id="engineer", resource_id="gmv", conditions={"min_clearance": 3})

    assert not can_read_metric(
        session, tenant_id=TENANT, roles=["engineer"], clearance=1, metric_name="gmv"
    ).allowed
    assert can_read_metric(
        session, tenant_id=TENANT, roles=["engineer"], clearance=3, metric_name="gmv"
    ).allowed


def test_密级条件写错时不误伤(session):
    add_policy(session, subject_id="engineer", resource_id="gmv", conditions={"min_clearance": "高"})

    assert can_read_metric(
        session, tenant_id=TENANT, roles=["engineer"], clearance=1, metric_name="gmv"
    ).allowed


def test_未知资源类型的策略不参与判定(session):
    add_policy(session, subject_id="engineer", resource_id="gmv", resource_type="document", effect="deny")

    assert can_read_metric(session, tenant_id=TENANT, roles=["engineer"], metric_name="gmv").allowed


# ------------------------------------------------------------------ 指标域与清单
def test_指标域与指标名分别判定(session):
    add_policy(session, subject_id="analyst", resource_id="finance", resource_type="metric_domain")

    assert can_read_domain(session, tenant_id=TENANT, roles=["analyst"], domain="finance").allowed
    assert not can_read_domain(session, tenant_id=TENANT, roles=["engineer"], domain="finance").allowed
    # 域策略不自动等于"域内每个指标都允许"：指标名要单独有策略（避免含糊的隐式授权）
    assert can_read_metric(session, tenant_id=TENANT, roles=["engineer"], metric_name="gmv").allowed


def test_问题文本识别受限指标域():
    assert domains_in_query("把含营收的经营大盘报表导给我") == ["finance"]
    assert domains_in_query("8 月各渠道 GMV 和收入分成明细") == ["finance"]
    # 普通问题不该被误判（关键词要精挑，否则会把正常问题一起拦掉）
    assert domains_in_query("3.2.0 发布后次日留存为什么下降") == []
    assert domains_in_query("8 月付费会员收入和 ARPU 是多少") == []


def test_域拒绝结果带上可解释原因(session):
    add_policy(session, subject_id="analyst", resource_id="finance", resource_type="metric_domain")

    denied = domain_denials(
        session, tenant_id=TENANT, roles=["engineer"], user_id="u-1", query="帮我拉一下营收大盘"
    )

    assert len(denied) == 1
    assert denied[0].allowed is False
    assert "finance" in denied[0].reason


def test_可见指标清单过滤(session):
    add_policy(session, subject_id="analyst", resource_id="order_conversion_rate")

    visible = visible_metrics(
        session,
        tenant_id=TENANT,
        roles=["engineer"],
        metric_names=["dau", "order_conversion_rate", "crash_rate"],
    )

    assert visible == ["dau", "crash_rate"]


def test_拒绝结果不包含任何数值(session):
    """拒绝信息只说明"无权访问"，绝不能把指标值顺带漏出去。"""
    add_policy(session, subject_id="analyst", resource_id="gmv", effect="deny")

    decision = evaluate_access(
        session,
        tenant_id=TENANT,
        roles=["engineer"],
        resource_type="metric",
        resource_name="gmv",
    )

    payload = decision.as_dict()
    assert payload["allowed"] is False
    assert "gmv" in payload["reason"]
    assert set(payload) == {
        "allowed",
        "reason",
        "resource_type",
        "resource_name",
        "matched_policy_id",
    }
    assert not re.search(r"\d", payload["reason"]), "拒绝原因里不该出现任何数字"

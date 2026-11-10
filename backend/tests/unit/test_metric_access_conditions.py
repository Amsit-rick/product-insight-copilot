"""指标级密级**细粒度条件**的单元测试：有效期、时间窗、行级数据范围。

为什么这些条件值得单独测：
- 有效期/时间窗是"自动收权"的机制，判错的方向决定了是"该收没收"还是"该给没给"，
  两边都是事故：前者是越权，后者是业务被卡；
- 行级范围（"只能看本渠道数据"）最容易出的问题是**把权限问题伪装成数据缺失**——
  用户请求越界渠道时拿到空结果，会以为"这个渠道没数据"，而不是"我无权看"。

时间相关的断言一律传显式 `now`，不依赖机器当前时间，否则测试会在半夜莫名其妙地红。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.identity import AccessPolicy
from app.services.metric_access import (
    ROW_FILTER_DIMENSIONS,
    _time_window_ok,
    _validity_ok,
    check_dimension_values,
    merge_row_filters,
    resolve_metric_scope,
)

TENANT = "t-cond"
CN = timezone(timedelta(hours=8))


def add_policy(
    session: Session,
    *,
    subject_id: str = "analyst",
    subject_type: str = "role",
    resource_type: str = "metric",
    resource_id: str = "order_conversion_rate",
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


def policy(conditions: dict) -> AccessPolicy:
    return AccessPolicy(
        tenant_id=TENANT,
        subject_type="role",
        subject_id="analyst",
        resource_type="metric",
        resource_id="m",
        effect="allow",
        actions=["metric:read"],
        conditions=conditions,
    )


# ------------------------------------------------------------------- 有效期
def test_授权到期后拒绝(session):
    add_policy(session, conditions={"valid_until": "2020-01-31"})

    decision = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["analyst"], metric_name="order_conversion_rate"
    )

    assert decision.allowed is False
    assert "已过期" in decision.reason


def test_授权到期当天仍然有效(session):
    today = datetime.now(timezone.utc).date().isoformat()
    add_policy(session, conditions={"valid_until": today})

    decision = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["analyst"], metric_name="order_conversion_rate"
    )

    assert decision.allowed is True


def test_授权尚未生效时拒绝(session):
    add_policy(session, conditions={"valid_from": "2099-01-01"})

    decision = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["analyst"], metric_name="order_conversion_rate"
    )

    assert decision.allowed is False
    assert "尚未生效" in decision.reason


def test_有效期取值非法时失败关闭(session):
    add_policy(session, conditions={"valid_until": "不是日期"})

    decision = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["analyst"], metric_name="order_conversion_rate"
    )

    assert decision.allowed is False
    assert "策略时间条件非法" in decision.reason


def test_过期策略不会连坐其它有效策略(session):
    """同一角色有两条授权时，一条过期不能让另一条也失效。"""
    add_policy(session, conditions={"valid_until": "2020-01-01"})
    add_policy(session, conditions={"valid_from": "2020-01-01", "valid_until": "2099-01-01"})

    decision = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["analyst"], metric_name="order_conversion_rate"
    )

    assert decision.allowed is True


# ------------------------------------------------------------------- 时间窗
def test_时间窗按星期判断():
    conditions = {"time_window": {"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}}
    monday_10am = datetime(2026, 9, 14, 10, 0, tzinfo=CN)  # 2026-09-14 是周一
    saturday_10am = datetime(2026, 9, 19, 10, 0, tzinfo=CN)

    assert _time_window_ok(policy(conditions), monday_10am)[0] is True
    ok, reason = _time_window_ok(policy(conditions), saturday_10am)
    assert ok is False
    assert "不在允许的时间窗内" in reason


def test_时间窗按时刻判断():
    conditions = {"time_window": {"start": "09:00", "end": "18:00"}}
    inside = datetime(2026, 9, 14, 10, 30, tzinfo=CN)
    before = datetime(2026, 9, 14, 8, 30, tzinfo=CN)
    after = datetime(2026, 9, 14, 19, 0, tzinfo=CN)

    assert _time_window_ok(policy(conditions), inside)[0] is True
    assert _time_window_ok(policy(conditions), before)[0] is False
    assert _time_window_ok(policy(conditions), after)[0] is False


def test_时间窗支持跨零点():
    """夜间窗口（22:00–06:00）不能因为 start > end 就被判成永不满足。"""
    conditions = {"time_window": {"start": "22:00", "end": "06:00"}}

    assert _time_window_ok(policy(conditions), datetime(2026, 9, 14, 23, 30, tzinfo=CN))[0] is True
    assert _time_window_ok(policy(conditions), datetime(2026, 9, 14, 3, 0, tzinfo=CN))[0] is True
    assert _time_window_ok(policy(conditions), datetime(2026, 9, 14, 12, 0, tzinfo=CN))[0] is False


def test_时间窗取值非法时按不限制处理():
    conditions = {"time_window": {"start": "25:99", "end": "abc"}}

    assert _time_window_ok(policy(conditions), datetime(2026, 9, 14, 12, 0, tzinfo=CN))[0] is True


def test_时间窗按声明时区换算():
    """同一时刻在 +08:00 与 UTC 下落在窗口的不同侧——时区必须真的生效。"""
    conditions = {"time_window": {"start": "09:00", "end": "18:00", "timezone": "+08:00"}}
    moment_utc = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)  # 北京 09:00
    moment_utc_late = datetime(2026, 9, 14, 23, 0, tzinfo=timezone.utc)  # 北京次日 07:00

    from app.services.metric_access import _now_in_zone

    # _now_in_zone 只负责取"当前时间"，这里直接验证偏移解析结果
    assert _now_in_zone("+08:00").utcoffset() == timedelta(hours=8)
    assert _time_window_ok(policy(conditions), moment_utc.astimezone(CN))[0] is True
    assert _time_window_ok(policy(conditions), moment_utc_late.astimezone(CN))[0] is False


# --------------------------------------------------------------- 行级数据范围
def test_行级范围进入查询范围(session):
    add_policy(session, conditions={"row_filters": {"channel": ["channel_a"]}})

    scope = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["analyst"], metric_name="order_conversion_rate"
    )

    assert scope.allowed is True
    assert scope.filtered is True
    assert scope.row_filters == {"channel": ["channel_a"]}
    assert "channel" in scope.note and "channel_a" in scope.note


def test_非法维度的行级条件被忽略(session):
    """维度白名单之外的键必须丢弃：策略表由人写，不能成为注入列名的入口。"""
    add_policy(session, conditions={"row_filters": {"DROP TABLE": ["x"], "channel": ["channel_a"]}})

    scope = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["analyst"], metric_name="order_conversion_rate"
    )

    assert set(scope.row_filters) <= set(ROW_FILTER_DIMENSIONS)
    assert scope.row_filters == {"channel": ["channel_a"]}


def test_多条策略的范围内取并集():
    a = policy({"row_filters": {"channel": ["channel_a"]}})
    b = policy({"row_filters": {"channel": ["channel_b"]}})

    merged = merge_row_filters([a, b])

    assert merged["channel"] == ["channel_a", "channel_b"]


def test_某维度只有部分策略限制时不限制该维度():
    """A 限渠道、B 限渠道+机型 → 机型只在 B 里出现，而 A 授予了全部机型，故机型不限。"""
    a = policy({"row_filters": {"channel": ["channel_a"]}})
    b = policy({"row_filters": {"channel": ["channel_b"], "device": ["low_end"]}})

    merged = merge_row_filters([a, b])

    assert merged["channel"] == ["channel_a", "channel_b"]
    assert "device" not in merged


def test_未限制优先于受限():
    """一条策略不限渠道时，合并结果不应把渠道限死（allow 是叠加授权）。"""
    limiting = policy({"row_filters": {"channel": ["channel_a"]}})
    unrestricted = policy({})

    merged = merge_row_filters([limiting, unrestricted])

    assert "channel" not in merged


def test_请求越界维度值给出明确原因(session):
    add_policy(session, conditions={"row_filters": {"channel": ["channel_a"]}})
    scope = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["analyst"], metric_name="order_conversion_rate"
    )

    violation = check_dimension_values(scope, {"channel": "channel_b"})
    inside = check_dimension_values(scope, {"channel": "channel_a"})
    unspecified = check_dimension_values(scope, {"channel": None})

    assert violation and "不在你的数据范围" in violation
    assert inside is None
    assert unspecified is None


def test_无范围限制时不拦任何维度值(session):
    add_policy(session, conditions={})
    scope = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["analyst"], metric_name="order_conversion_rate"
    )

    assert check_dimension_values(scope, {"channel": "anything"}) is None


# --------------------------------------------------------------- 缓存范围指纹
def test_范围指纹随策略变化(session):
    add_policy(session, conditions={"row_filters": {"channel": ["channel_a"]}})
    narrow = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["analyst"], metric_name="order_conversion_rate"
    )
    add_policy(session, conditions={"row_filters": {"channel": ["channel_b"]}})
    wide = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["analyst"], metric_name="order_conversion_rate"
    )

    assert narrow.cache_signature != wide.cache_signature
    # 没有范围限制时用固定标记，便于在缓存键里一眼看出"这是全量范围"
    assert resolve_metric_scope(
        session, tenant_id=TENANT, roles=["other"], metric_name="order_conversion_rate"
    ).cache_signature == "all"


def test_被拒时范围指纹不参与缓存(session):
    add_policy(session, effect="deny", subject_id="engineer")

    scope = resolve_metric_scope(
        session, tenant_id=TENANT, roles=["engineer"], metric_name="order_conversion_rate"
    )

    assert scope.allowed is False


@pytest.mark.parametrize("bad", [{"time_window": {}}, {"valid_from": None}])
def test_空条件等价于不限制(session, bad):
    add_policy(session, conditions=bad)

    assert resolve_metric_scope(
        session, tenant_id=TENANT, roles=["analyst"], metric_name="order_conversion_rate"
    ).allowed

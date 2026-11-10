"""服务层测试：行级数据范围必须真的落在 SQL 上，并且不把权限问题伪装成数据缺失。

判定层（metric_access）说"这个角色只能看 channel_a"很容易通过；
真正会出事的是**查询没带过滤条件**——数据照样吐出来，而且没人会发现。
所以这里用真实库、真实查询去断言返回的行到底属于哪个渠道。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.metrics.schemas import BreakdownRequest, TrendRequest
from app.metrics.service import SqlMetricService
from app.models.identity import AccessPolicy

TENANT = "*"  # 与 conftest 的 add_snapshot 默认租户一致
METRIC = "retention_d1"


def add_policy(session: Session, *, subject_id: str, conditions: dict) -> AccessPolicy:
    row = AccessPolicy(
        tenant_id=TENANT,
        subject_type="role",
        subject_id=subject_id,
        resource_type="metric",
        resource_id=METRIC,
        effect="allow",
        actions=["metric:read"],
        conditions=conditions,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture()
def seeded(session, add_snapshot):
    """造两个渠道的数据：channel_a 值是 0.10，channel_b 值是 0.90（差值明显，便于断言）。"""
    base = date(2026, 8, 1)
    for offset in range(3):
        add_snapshot(stat_date=base + timedelta(days=offset), value=0.10, metric_name=METRIC, channel="channel_a")
        add_snapshot(stat_date=base + timedelta(days=offset), value=0.90, metric_name=METRIC, channel="channel_b")
    return {"start": base, "end": base + timedelta(days=2)}


def service(session, roles: list[str]) -> SqlMetricService:
    return SqlMetricService(session, tenant_id=TENANT, roles=roles, user_id="u-1", clearance=1)


def test_未受限时能看到全部渠道(session, seeded):
    add_policy(session, subject_id="analyst", conditions={})  # 明确授权且不限范围

    result = service(session, ["analyst"]).get_trend(
        TrendRequest(metric_name=METRIC, start_date=seeded["start"], end_date=seeded["end"])
    )

    # 两个渠道同一天：按样本量加权平均后落在两者之间
    assert result.rows
    assert all(0.10 <= row["value"] <= 0.90 for row in result.rows)
    assert not any("数据范围" in warning for warning in result.warnings)


def test_未列出的角色在指标有策略后即无权(session, seeded):
    """白名单语义：指标一旦有人配了策略，没被明确允许的角色就查不到——这正是它要的效果。"""
    add_policy(session, subject_id="scoped", conditions={"row_filters": {"channel": ["channel_a"]}})

    result = service(session, ["analyst"]).get_trend(
        TrendRequest(metric_name=METRIC, start_date=seeded["start"], end_date=seeded["end"])
    )

    assert result.rows == []
    assert result.summary.get("policy_denied") is True


def test_行级范围把结果限制在允许的渠道(session, seeded):
    add_policy(session, subject_id="scoped", conditions={"row_filters": {"channel": ["channel_a"]}})

    result = service(session, ["scoped"]).get_trend(
        TrendRequest(metric_name=METRIC, start_date=seeded["start"], end_date=seeded["end"])
    )

    assert result.rows, "受限角色仍然应该能看到范围内那一份数据"
    assert all(row["value"] == pytest.approx(0.10) for row in result.rows), "只应看到 channel_a 的值"
    assert any("已按数据范围策略过滤" in w for w in result.warnings), "必须告知结果被过滤过"


def test_请求越界渠道时给出权限原因而不是空数据(session, seeded):
    add_policy(session, subject_id="scoped", conditions={"row_filters": {"channel": ["channel_a"]}})

    result = service(session, ["scoped"]).get_trend(
        TrendRequest(
            metric_name=METRIC,
            start_date=seeded["start"],
            end_date=seeded["end"],
            channel="channel_b",
        )
    )

    assert result.rows == []
    assert result.summary.get("policy_denied") is True
    assert result.summary.get("data_range_denied") is True
    assert any("超出你的数据范围" in w for w in result.warnings)
    assert "channel_b" in result.summary["reason"]


def test_分群只返回范围内的分群(session, seeded):
    add_policy(session, subject_id="scoped", conditions={"row_filters": {"channel": ["channel_a"]}})

    result = service(session, ["scoped"]).breakdown(
        BreakdownRequest(
            metric_name=METRIC,
            start_date=seeded["start"],
            end_date=seeded["end"],
            dimension="channel",
        )
    )

    keys = {row["key"] for row in result.rows}
    assert keys == {"channel_a"}, f"分群里出现了无权渠道: {keys}"


def test_覆盖度按范围统计(session, seeded):
    add_policy(session, subject_id="scoped", conditions={"row_filters": {"channel": ["channel_a"]}})
    add_policy(session, subject_id="analyst", conditions={})

    scoped = service(session, ["scoped"]).coverage(METRIC)
    unscoped = service(session, ["analyst"]).coverage(METRIC)

    assert scoped["rows"] == 3, "只统计 channel_a 的 3 行"
    assert unscoped["rows"] == 6
    assert "已按数据范围策略过滤" in scoped.get("data_range_note", "")
    assert "data_range_note" not in unscoped, "无范围限制时不必加说明"


def test_版本列表按范围统计(session, seeded, add_snapshot):
    base = date(2026, 9, 1)
    for channel, version in (("channel_a", "3.2.0"), ("channel_b", "3.2.0")):
        add_snapshot(stat_date=base, value=0.5, metric_name=METRIC, channel=channel, app_version=version)
    add_policy(session, subject_id="scoped", conditions={"row_filters": {"channel": ["channel_a"]}})
    add_policy(session, subject_id="analyst", conditions={})

    scoped = service(session, ["scoped"]).list_versions(METRIC)
    unscoped = service(session, ["analyst"]).list_versions(METRIC)

    assert scoped[0]["rows"] == 1
    assert unscoped[0]["rows"] == 2


def test_授权到期后查不到数据(session, seeded):
    """有效期条件必须在查询路径上生效，而不只是判定函数里的一个布尔值。"""
    add_policy(
        session, subject_id="temp", conditions={"valid_until": "2020-01-01"}
    )

    result = service(session, ["temp"]).get_trend(
        TrendRequest(metric_name=METRIC, start_date=seeded["start"], end_date=seeded["end"])
    )

    assert result.rows == []
    assert result.summary.get("policy_denied") is True
    assert "已过期" in result.summary["reason"]


def test_不同范围的角色不会命中彼此的缓存(session, seeded):
    """缓存键里必须带范围指纹：否则放宽/收紧策略后会把旧范围内的数据发出去。"""
    add_policy(session, subject_id="scoped", conditions={"row_filters": {"channel": ["channel_a"]}})
    add_policy(session, subject_id="wild", conditions={"row_filters": {"channel": ["channel_b"]}})

    narrow = service(session, ["scoped"]).get_trend(
        TrendRequest(metric_name=METRIC, start_date=seeded["start"], end_date=seeded["end"])
    )
    other = service(session, ["wild"]).get_trend(
        TrendRequest(metric_name=METRIC, start_date=seeded["start"], end_date=seeded["end"])
    )

    assert all(row["value"] == pytest.approx(0.10) for row in narrow.rows)
    assert all(row["value"] == pytest.approx(0.90) for row in other.rows)


def test_策略放宽后旧缓存失效(session, seeded):
    """先以窄范围查一次（写缓存），放宽策略后再查必须能拿到新范围内的数据。"""
    policy = add_policy(session, subject_id="scoped", conditions={"row_filters": {"channel": ["channel_a"]}})

    first = service(session, ["scoped"]).get_trend(
        TrendRequest(metric_name=METRIC, start_date=seeded["start"], end_date=seeded["end"])
    )
    assert all(row["value"] == pytest.approx(0.10) for row in first.rows)

    policy.conditions = {"row_filters": {"channel": ["channel_a", "channel_b"]}}
    session.flush()

    second = service(session, ["scoped"]).get_trend(
        TrendRequest(metric_name=METRIC, start_date=seeded["start"], end_date=seeded["end"])
    )

    # 范围变宽后，同一天的加权平均值会明显高于只有 channel_a 时的 0.10
    assert any(row["value"] > 0.10 for row in second.rows), "策略放宽后仍返回旧缓存 → 缓存键缺范围指纹"

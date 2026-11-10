"""政策生效期与适用范围判定的单测（需求书 §4.3）。

这段逻辑是**合规闸门**：判定错了会出现"引用已失效政策赔付"或"明明有政策却用不上"。
所以逐条钉住状态与范围的边界，包括"维度未知时保守判为不适用"这条容易写反的规则。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.models.enums import PolicyStatus
from app.models.ticket import PolicyDocument
from app.services.policy_service import _matches, applicability


def make_policy(**overrides) -> PolicyDocument:
    base = {
        "tenant_id": "t1",
        "title": "物流时效与补偿标准",
        "version": "v3.0",
        "scope": {"platform": "全平台", "shop_id": None},
        "effective_from": datetime(2026, 7, 1),
        "effective_to": None,
        "status": PolicyStatus.ACTIVE.value,
    }
    base.update(overrides)
    return PolicyDocument(**base)


def test_active_policy_is_usable():
    usable, reason = applicability(make_policy(), at=datetime(2026, 8, 20))
    assert usable is True
    assert reason == "适用"


def test_expired_policy_is_not_usable():
    row = make_policy(
        version="v1.0",
        effective_from=datetime(2025, 1, 1),
        effective_to=datetime(2026, 6, 30, 23, 59, 59),
        status=PolicyStatus.EXPIRED.value,
    )
    usable, reason = applicability(row, at=datetime(2026, 8, 20))
    assert usable is False
    assert "已失效" in reason and "2026-06-30" in reason


def test_future_policy_is_not_usable():
    row = make_policy(
        version="v4.0",
        effective_from=datetime(2027, 1, 1),
        status=PolicyStatus.DRAFT.value,
    )
    usable, reason = applicability(row, at=datetime(2026, 8, 20))
    assert usable is False
    assert "未生效" in reason


def test_archived_policy_is_not_usable_even_if_in_window():
    row = make_policy(status=PolicyStatus.ARCHIVED.value)
    usable, reason = applicability(row, at=datetime(2026, 8, 20))
    assert usable is False
    assert "归档" in reason


def test_boundary_dates_are_inclusive():
    """"生效当日可用、失效当日仍可用" —— 政策生效期要按日包含两端。"""
    start = datetime(2026, 7, 1)
    row = make_policy(effective_from=start, effective_to=datetime(2026, 7, 31, 23, 59, 59))
    assert applicability(row, at=start)[0] is True
    assert applicability(row, at=datetime(2026, 7, 31, 12))[0] is True
    assert applicability(row, at=datetime(2026, 8, 1))[0] is False


def test_scope_platform_must_match():
    row = make_policy(scope={"platform": "Android"})
    assert applicability(row, at=datetime(2026, 8, 20), platform="Android")[0] is True
    usable, reason = applicability(row, at=datetime(2026, 8, 20), platform="iOS")
    assert usable is False and "仅适用平台" in reason


def test_wildcard_scope_accepts_anything():
    for scope_value in ("全平台", "all", "*", "", ["全平台"]):
        row = make_policy(scope={"platform": scope_value})
        assert applicability(row, at=datetime(2026, 8, 20), platform="iOS")[0] is True


def test_unknown_dimension_is_conservative():
    """政策限定了店铺、但工单没有店铺信息 → **不适用**（宁可不用，不可用错）。"""
    row = make_policy(scope={"platform": "全平台", "shop_id": "self_operated"})
    usable, reason = applicability(row, at=datetime(2026, 8, 20), shop_id=None)
    assert usable is False
    assert "当前工单店铺为 未知" in reason
    # 店铺对得上才可用
    assert applicability(row, at=datetime(2026, 8, 20), shop_id="self_operated")[0] is True


def test_matches_accepts_list_and_scalar():
    assert _matches(["a", "b"], "b") is True
    assert _matches("a", "a") is True
    assert _matches(None, "a") is True
    assert _matches([], "a") is True
    assert _matches("a", None) is False


def test_applicability_at_now_uses_current_time():
    """不传 at 时按当前时间判定：过期政策不会因为"忘了传时间"而被放行。"""
    past = datetime.now() - timedelta(days=400)
    row = make_policy(
        effective_from=past,
        effective_to=past + timedelta(days=30),
        status=PolicyStatus.EXPIRED.value,
    )
    assert applicability(row)[0] is False

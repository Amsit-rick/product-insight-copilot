"""指标数据范围（细粒度密级）的实跑演示：离线、确定性、不花钱。

在一个临时库里导入演示指标数据，然后依次演示四种策略行为：

1. 无策略 → 全量可见；
2. 行级范围（只能看 channel_a）→ 结果被裁剪，并说明"已按数据范围策略过滤"；
3. 显式请求越界渠道（channel_b）→ **拒绝并说明原因**，而不是返回空数据；
4. 授权有效期已过 / 不在允许时间窗 → 拒绝并说明"什么时候能用"。

用法（在 backend 目录下）：

    ..\\.venv\\Scripts\\python.exe scripts\\demo_metric_scope.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.seed import seed_all  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.metrics.schemas import BreakdownRequest, TrendRequest  # noqa: E402
from app.metrics.service import SqlMetricService  # noqa: E402
from app.models.identity import AccessPolicy, Tenant  # noqa: E402

METRIC = "crash_rate"
START = "2026-08-01"
END = "2026-08-31"
ROLE = "product_manager"


def add_policy(session: Session, tenant_id: str, *, resource_id: str, conditions: dict) -> None:
    session.add(
        AccessPolicy(
            tenant_id=tenant_id,
            subject_type="role",
            subject_id=ROLE,
            resource_type="metric",
            resource_id=resource_id,
            effect="allow",
            actions=["metric:read"],
            conditions=conditions,
        )
    )
    session.commit()


def show(session: Session, tenant_id: str, *, title: str, channel: str | None = None) -> None:
    service = SqlMetricService(
        session, tenant_id=tenant_id, roles=[ROLE], user_id="u-demo", clearance=1
    )
    result = service.get_trend(
        TrendRequest(
            metric_name=METRIC,
            start_date=date.fromisoformat(START),
            end_date=date.fromisoformat(END),
            channel=channel,
        )
    )
    print(f"\n--- {title}")
    if result.summary.get("policy_denied"):
        print(f"    拒绝：{result.summary.get('reason')}")
        return
    values = [round(row["value"], 4) for row in result.rows[:4]]
    print(f"    返回 {len(result.rows)} 个数据点，前几个值：{values}")
    # 渠道清单最直观：被范围策略裁剪时，这里只会剩下允许的渠道
    breakdown = service.breakdown(
        BreakdownRequest(
            metric_name=METRIC,
            start_date=date.fromisoformat(START),
            end_date=date.fromisoformat(END),
            dimension="channel",
        )
    )
    print(f"    可见渠道：{[row['key'] for row in breakdown.rows]}")
    for warning in result.warnings:
        print(f"    提示：{warning}")


def main() -> int:
    settings = get_settings()
    tmp = BACKEND / ".tmp" / "metric_scope_demo.db"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        tmp.unlink()
    engine = build_engine(
        settings.model_copy(
            update={
                "database_url": f"sqlite:///{tmp.as_posix()}",
                "llm_provider": "stub",
                "embedding_provider": "stub",
                # 关掉缓存：演示要看到"每次都真查",否则第二次会命中缓存看不出差别
                "metric_cache_ttl_seconds": 0,
            }
        )
    )
    from app.models import Base

    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    session = session_factory()
    try:
        seed_all(session=session, with_eval_cases=False)
        tenant = session.query(Tenant).filter(Tenant.code == "demo").first()
        tenant_id = tenant.id

        print("=== 指标数据范围（行级密级）演示 ===")
        print(f"  指标：{METRIC}  窗口：{START} ~ {END}  角色：{ROLE}")

        show(session, tenant_id, title="① 无策略：全量可见（未指定渠道）")
        show(session, tenant_id, title="① 无策略：指定 channel_b 也能查", channel="channel_b")

        add_policy(session, tenant_id, resource_id=METRIC, conditions={"row_filters": {"channel": ["channel_a"]}})
        show(session, tenant_id, title="② 行级范围 channel_a：结果被裁剪")
        show(session, tenant_id, title="② 行级范围 channel_a：请求 channel_b 被拒", channel="channel_b")

        # 换一条带有效期的策略（把范围策略删掉，避免并集语义把限制放开）
        session.query(AccessPolicy).filter(AccessPolicy.resource_id == METRIC).delete()
        session.commit()
        add_policy(
            session,
            tenant_id,
            resource_id=METRIC,
            conditions={"valid_from": "2020-01-01", "valid_until": "2020-12-31"},
        )
        show(session, tenant_id, title="③ 授权已过期：拒绝并说明")

        session.query(AccessPolicy).filter(AccessPolicy.resource_id == METRIC).delete()
        session.commit()
        # 造一个"永远不在当前时间"的时间窗：凌晨 03:00–03:01
        add_policy(
            session,
            tenant_id,
            resource_id=METRIC,
            conditions={"time_window": {"start": "03:00", "end": "03:01", "timezone": "+08:00"}},
        )
        show(session, tenant_id, title="④ 不在允许的时间窗（03:00–03:01）：拒绝并说明")

        print("\n=== 结论 ===")
        print("  · 行级范围在 SQL 层生效：越界数据不会出现在结果里")
        print("  · 越界请求返回明确原因，而不是空数据（否则会被误读成'这个渠道没数据'）")
        print("  · 有效期与时间窗让授权可以自动收敛，不需要人工回来删策略")
    finally:
        session.close()
        engine.dispose()
        if tmp.exists():
            tmp.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

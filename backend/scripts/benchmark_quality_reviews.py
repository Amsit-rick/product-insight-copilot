"""在隔离 SQLite 合成库上测量质检摘要查询路径。

这不是生产压测，也不会连接演示库、真实模型或外部平台。脚本只用于比较本地
摘要字段筛选、问题标记筛选和分页读取的查询开销，输出结果不能替代生产容量结论。
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return round(ordered[index], 3)


def build_fixture(session: Session, count: int) -> None:
    from app.models.ticket import Ticket

    from app.models.base import utcnow

    now = utcnow()
    statuses = ("unreviewed", "pass", "needs_followup", "block")
    issues = ("missing_evidence", "wrong_policy", "unsafe_reply", "wrong_action")
    for start in range(0, count, 500):
        rows = []
        for index in range(start, min(start + 500, count)):
            status = statuses[index % len(statuses)]
            code = issues[index % len(issues)]
            rows.append(
                Ticket(
                    tenant_id="benchmark-tenant",
                    external_id=f"BENCH-{index:06d}",
                    customer_id=f"customer-{index:06d}",
                    order_id=f"ORDER-{index:06d}",
                    subject=f"合成质检工单 {index}",
                    content="仅用于质检查询基准，不代表真实客户数据。",
                    channel="benchmark",
                    status="ready_to_reply",
                    priority="high" if index % 5 == 0 else "medium",
                    quality_status=status,
                    quality_issue_codes=[code],
                    quality_reviewed_at=None if status == "unreviewed" else now - timedelta(seconds=index),
                    quality_reviewer_id=None if status == "unreviewed" else "benchmark-reviewer",
                )
            )
        session.add_all(rows)
        session.commit()


def measure(session: Session, repeats: int) -> dict:
    from app.services.ticket_service import TicketService

    service = TicketService(session)
    cases = {
        "status_filter": {"quality_status": "block", "limit": 20, "offset": 0},
        "issue_filter": {"quality_issue_code": "unsafe_reply", "limit": 20, "offset": 0},
        "paged_all": {"limit": 20, "offset": 100},
    }
    report = {}
    for name, options in cases.items():
        elapsed = []
        total = 0
        for _ in range(repeats):
            started = time.perf_counter()
            rows, total, _ = service.list_quality_tickets(tenant_id="benchmark-tenant", **options)
            elapsed.append((time.perf_counter() - started) * 1000)
            if len(rows) != 20:
                raise RuntimeError(f"{name} 未返回固定分页数量：{len(rows)}")
        report[name] = {"total": total, "p50_ms": percentile(elapsed, 0.50), "p95_ms": percentile(elapsed, 0.95)}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="隔离测量质检摘要查询")
    parser.add_argument("--tickets", type=int, default=5000, choices=range(100, 20001))
    parser.add_argument("--repeats", type=int, default=5, choices=range(1, 101))
    args = parser.parse_args(argv)

    from app.models.base import Base
    from app.models.ticket import Ticket  # noqa: F401 - 注册表结构

    with tempfile.TemporaryDirectory(prefix="quality-benchmark-") as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'benchmark.db'}")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            build_fixture(session, args.tickets)
            report = measure(session, args.repeats)
        engine.dispose()
    print("=== 质检查询隔离基准（合成数据，仅用于本地工程比较） ===")
    print(f"工单数={args.tickets} 重复次数={args.repeats}")
    for name, metrics in report.items():
        print(f"{name}: total={metrics['total']} p50={metrics['p50_ms']}ms p95={metrics['p95_ms']}ms")
    print("注意：该结果不代表生产容量；生产结论必须使用目标数据库、真实数据分布和并发压测。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

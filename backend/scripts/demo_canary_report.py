"""灰度效果对比的实跑演示（离线、确定性、不调用任何模型）。

为什么要有这个脚本：灰度对比的输出是"给人做选型决策"看的，光有单测不够——
需要能**一眼看到结论长什么样**，包括最关键的"样本不足时它长什么样"。

它在一个临时库里造两组（灰度 / 对照）模拟运行数据，然后打印对比结果：

    python scripts/demo_canary_report.py                 # 默认：两侧各 8 个任务，差异明显
    python scripts/demo_canary_report.py --canary 2 --control 2   # 演示"样本不足"的输出
    python scripts/demo_canary_report.py --canary 20 --control 20 --no-difference  # 两组几乎一样

临时库写在 backend/.tmp/ 下，用完即删，**不碰开发库、不花一分钱**。
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sqlalchemy.orm import Session  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.models import Base  # noqa: E402
from app.models.base import utcnow  # noqa: E402
from app.models.workflow import AgentRun, AnalysisTask, Report  # noqa: E402
from app.services.canary_report import compare  # noqa: E402

TENANT = "demo-tenant"


def seed(session: Session, *, canary: int, control: int, no_difference: bool) -> None:
    """造两组样本：灰度侧的"结论条数"与"引用完整度"更高（除非要求两组一致）。"""
    for index in range(canary + control):
        is_canary = index < canary
        task = AnalysisTask(
            tenant_id=TENANT,
            user_id="u-demo",
            query=f"灰度演示任务 {index}",
            title=f"灰度演示任务 {index}",
            status="completed",
            created_at=utcnow() - timedelta(hours=index),
        )
        session.add(task)
        session.flush()

        findings = 6 if is_canary else (6 if no_difference else 3)
        latency = 900 if is_canary else (1400 if no_difference else 1600)
        tokens = 2200 if is_canary else (2200 if no_difference else 1800)
        session.add(
            AgentRun(
                task_id=task.id,
                agent_code="report_agent",
                node="report_agent",
                status="succeeded",
                model="deepseek-reasoner-v2" if is_canary else "deepseek-reasoner",
                output={
                    "model_source": "canary" if is_canary else "override",
                    "model_used": True,
                    "schema_ok": True if is_canary or no_difference else False,
                    "findings": [{"text": f"结论{i}"} for i in range(findings)],
                },
                latency_ms=latency,
                prompt_tokens=tokens - 800,
                completion_tokens=800,
                total_tokens=tokens,
                created_at=utcnow(),
            )
        )
        facts = 6 if is_canary else (6 if no_difference else 4)
        cited = is_canary or no_difference
        session.add(
            Report(
                task_id=task.id,
                tenant_id=TENANT,
                version=1,
                summary="演示摘要",
                facts=[
                    {"text": f"事实{i}", "evidence_refs": ["E1"] if cited else []}
                    for i in range(facts)
                ],
                confidence=0.72 if is_canary else (0.72 if no_difference else 0.55),
                needs_human_review=not is_canary and not no_difference,
                priority="P1",
            )
        )
    session.commit()


def print_group(stats: dict) -> None:
    label = "灰度（canary）" if stats["label"] == "canary" else "对照（control）"
    print(f"\n  【{label}】任务 {stats['task_count']} 个 / Agent 运行 {stats['run_count']} 次")
    print(f"    模型：{'、'.join(stats['models']) or '（无）'}")
    print(
        f"    模型调用率 {stats['model_used_rate']:.0%}   Schema 合规率 {stats['schema_ok_rate']:.0%}"
        f"   错误率 {stats['error_rate']:.0%}"
    )
    print(
        f"    平均结论数 {stats['avg_findings']}    延迟 p50 {stats['latency_p50_ms']:.0f}ms"
        f" / p95 {stats['latency_p95_ms']:.0f}ms"
    )
    print(
        f"    Token {stats['total_tokens']}    成本 {stats['total_cost']:.4f}"
        f"（每任务 {stats['cost_per_task']:.4f}）"
    )
    print(
        f"    报告 {stats['report_count']} 份   平均事实 {stats['avg_facts_per_report']}"
        f"    事实带引用率 {stats['cited_fact_rate']:.0%}"
        f"    转人工率 {stats['human_review_rate']:.0%}    平均置信度 {stats['avg_confidence']}"
    )


def print_metrics(title: str, items: list[dict]) -> None:
    if not items:
        print(f"\n  {title}：无（两侧样本不足）")
        return
    print(f"\n  {title}")
    print(f"    {'指标':<22}{'灰度':>10}{'对照':>10}{'差异':>11}   结论")
    for item in items:
        # 比率类用百分点（对照为 0 时百分比没意义），均值类用相对百分比
        if item.get("test") == "two-proportion-z":
            delta = f"{item.get('delta_pp', 0.0):+.1f}pp"
        else:
            delta = f"{item['delta_pct']:+.1f}%"
        p_value = f" (p={item['p_value']})" if item["p_value"] is not None else ""
        print(
            f"    {item['metric']:<22}{item['canary']:>10}{item['control']:>10}{delta:>11}   "
            f"{item['verdict']}{p_value}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="灰度效果对比演示（离线、不花钱）")
    parser.add_argument("--canary", type=int, default=8, help="灰度侧任务数")
    parser.add_argument("--control", type=int, default=8, help="对照侧任务数")
    parser.add_argument("--no-difference", action="store_true", help="造两组几乎一样的数据")
    args = parser.parse_args(argv)

    settings = get_settings()
    tmp = BACKEND / ".tmp" / "canary_demo.db"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        tmp.unlink()
    engine = build_engine(
        settings.model_copy(update={"database_url": f"sqlite:///{tmp.as_posix()}"})
    )
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    session = session_factory()
    try:
        seed(session, canary=args.canary, control=args.control, no_difference=args.no_difference)
        # 演示用配置：把候选模型单价设得更高，顺便体现"更贵但更好"也能被看见
        demo_settings: Settings = settings.model_copy(
            update={
                "model_canary": {"report_agent": {"model": "deepseek-reasoner-v2", "percent": 50}},
                "canary_min_sample": 5,
                "model_prices": {
                    "deepseek-reasoner": {"input": 4.0, "output": 16.0},
                    "deepseek-reasoner-v2": {"input": 6.0, "output": 24.0},
                },
            }
        )
        result = compare(session, TENANT, days=7, settings=demo_settings)
    finally:
        session.close()
        engine.dispose()
        if tmp.exists():
            tmp.unlink()

    print("=== 灰度效果对比（离线演示数据，非真实模型调用）===")
    print(f"  窗口 {result['window_days']} 天    样本门槛：每侧 ≥ {result['min_sample']} 个任务")
    print_group(result["groups"]["canary"])
    print_group(result["groups"]["control"])
    print_metrics("过程指标", result["metrics"])
    print_metrics("报告质量指标", result["report_metrics"])

    print(f"\n=== 结论 ===\n  {result['verdict']}：{result['verdict_reason']}")
    print("\n=== 口径与局限（结论引用时必须带上）===")
    print(f"  分组：{result['method']['grouping']}")
    print(f"  比率：{result['method']['rates']}")
    print(f"  均值：{result['method']['means']}")
    for item in result["method"]["limitations"]:
        print(f"  - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

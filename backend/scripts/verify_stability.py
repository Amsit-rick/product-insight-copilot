"""阶段六验收脚本：稳定性扩展（成本核算 / 限额熔断 / 缓存失效 / 并发与限流）实跑验证。

跑**两个真实任务**（同一个问题连跑两次），验证四件事：

1. **不锁库**：事件流里不能出现 `database is locked` / `OperationalError`；
2. **成本算得出来**：`agent_runs` 的 token 用量能汇总成金额，且预算状态可读；
3. **缓存真的命中**：第二次同问题检索命中缓存（指纹失效策略自动生效）；
4. **模型路由生效**：报告 Agent 用强模型，其余用快模型，且降级时如实标注。

用法（会真实调用大模型，请确认额度）::

    python scripts/verify_stability.py            # 实跑两个任务
    python scripts/verify_stability.py --dry-run  # 只打印配置与路由，不花钱
    python scripts/verify_stability.py --once     # 只跑一个任务（省一半费用）

退出码 0 表示全部断言通过；1 表示有断言失败（明细打印在末尾）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.identity import User  # noqa: E402
from app.models.workflow import AgentRun, AnalysisEvent, AnalysisTask  # noqa: E402
from app.services import caching, cost  # noqa: E402
from app.services.cache import get_cache  # noqa: E402
from app.services.limits import get_rate_limiter  # noqa: E402
from app.services.model_gateway import get_model_gateway  # noqa: E402
from app.services.task_service import TaskService  # noqa: E402
from app.services.workflow_runner import WorkflowRunner  # noqa: E402

QUERY = (
    "Android 端 3.2.0 发布后次日留存下降，哪些用户受到影响？"
    "版本变更、用户反馈和故障记录是否支持版本相关性？"
)
FILTERS = {
    "platform": "Android",
    "app_version": "3.2.0",
    "date_from": "2026-08-01",
    "date_to": "2026-08-31",
    "knowledge_bases": ["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
    "metrics": ["next_day_retention", "crash_rate"],
}
LOCK_MARKERS = ("database is locked", "OperationalError", "is locked")


def print_limits(settings) -> None:
    print("=== 限额 / 熔断 / 缓存配置 ===")
    print(
        f"  模型并发上限={settings.model_max_concurrency}"
        f" 等待超时={settings.model_concurrency_timeout_seconds}s"
        f"  单任务 token 上限={settings.task_token_limit}"
    )
    print(
        f"  单任务成本上限={settings.task_cost_limit} {settings.currency}"
        f"  租户日成本上限={settings.tenant_daily_cost_limit} {settings.currency}"
        f"  超限动作={settings.budget_exceeded_action}"
    )
    print(
        f"  提交限流={settings.task_submit_rate_limit_per_minute}/分钟"
        f"  检索缓存 TTL={settings.retrieval_cache_ttl_seconds}s"
        f"  指标缓存 TTL={settings.metric_cache_ttl_seconds}s"
    )
    print(f"  模型降级链={settings.llm_fallback_models or '（未配置）'}")
    cache = get_cache()
    print(f"  缓存后端={cache.describe()}")
    limiter = get_rate_limiter()
    print(f"  限流器后端={type(limiter.cache).__name__ if hasattr(limiter, 'cache') else limiter}")


def print_routing() -> None:
    gateway = get_model_gateway()
    print("\n=== 各 Agent 解析到的模型 ===")
    for item in gateway.routing_table():
        print(
            f"  {item['key']:16} model={item['model']:20} source={item['source']:9} "
            f"max_output={item.get('max_output_tokens')}"
        )
    for key in ("intent_classifier", "answerability_check"):
        item = gateway.profile_for(key).describe()
        print(f"  {key:16} model={item['model']:20} source={item['source']:9}")


def run_one(label: str) -> dict:
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.username == "pm").first()
        if user is None:
            raise SystemExit("缺少演示用户 pm，请先执行 python -m app.db.seed")
        task, _ = TaskService(session).create(user=user, query=QUERY, filters=FILTERS)
        session.commit()
        task_id = task.id
        tenant_id = task.tenant_id
    finally:
        session.close()

    print(f"\n=== [{label}] 任务 {task_id} 开始，真实调用模型 ===")
    before = caching.cache_stats()
    started = time.perf_counter()
    WorkflowRunner().run(task_id)
    elapsed = time.perf_counter() - started

    session = SessionLocal()
    try:
        task = session.get(AnalysisTask, task_id)
        runs = (
            session.query(AgentRun)
            .filter(AgentRun.task_id == task_id)
            .order_by(AgentRun.created_at)
            .all()
        )
        events = session.query(AnalysisEvent).filter(AnalysisEvent.task_id == task_id).all()
        breakdown = cost.task_cost(session, task_id)
        budget = cost.budget_state(session, tenant_id)
        rows = []
        for run in runs:
            output = run.output or {}
            rows.append(
                {
                    "agent": run.agent_code,
                    "model": run.model,
                    "status": run.status,
                    "model_used": output.get("model_used"),
                    "findings": len(output.get("findings") or []),
                    "prompt_tokens": run.prompt_tokens,
                    "completion_tokens": run.completion_tokens,
                    "cache_hit_tokens": int(output.get("cache_hit_tokens") or 0),
                    "error": (str(run.error)[:80] if run.error else None),
                }
            )
        lock_events = [
            e.message for e in events if any(m in (e.message or "") for m in LOCK_MARKERS)
        ]
        result = {
            "task_id": task_id,
            "status": task.status,
            "current_node": task.current_node,
            "waiting_reason": task.waiting_reason,
            "error": task.error,
            "elapsed_seconds": round(elapsed, 2),
            "cache_delta": {k: caching.cache_stats()[k] - before[k] for k in before},
            "cost": breakdown.as_dict(),
            "budget": budget,
            "agents": rows,
            "lock_events": lock_events,
            "event_count": len(events),
        }
    finally:
        session.close()

    print(f"  状态={result['status']} 节点={result['current_node']} 耗时={result['elapsed_seconds']}s")
    print(
        f"  成本={result['cost']['amount']} {result['cost']['currency']}"
        f"  tokens={result['cost']['total_tokens']}"
        f"  (in={result['cost']['input_tokens']} out={result['cost']['output_tokens']}"
        f" cache_hit={result['cost']['cache_hit_tokens']})"
    )
    print(f"  缓存增量={result['cache_delta']}")
    for row in result["agents"]:
        print(
            f"    {row['agent']:16} model={row['model']:18} used={row['model_used']!s:5} "
            f"tokens={row['prompt_tokens']}/{row['completion_tokens']} 结论={row['findings']} "
            f"{'错误=' + row['error'] if row['error'] else ''}"
        )
    if result["lock_events"]:
        print(f"  !! 出现锁相关事件 {len(result['lock_events'])} 条: {result['lock_events'][:2]}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段六稳定性实跑验证")
    parser.add_argument("--dry-run", action="store_true", help="只打印配置与路由，不调用模型")
    parser.add_argument("--once", action="store_true", help="只跑一个任务")
    args = parser.parse_args()

    settings = get_settings()
    print_limits(settings)
    print_routing()
    if args.dry_run:
        print("\n[dry-run] 未调用模型。")
        return 0

    first = run_one("第 1 次")
    second = None if args.once else run_one("第 2 次（验缓存命中）")

    print("\n=== 断言 ===")
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    for label, item in (("第1次", first), ("第2次", second)):
        if item is None:
            continue
        check(f"{label}无锁库事件", not item["lock_events"], str(item["lock_events"][:1]))
        check(f"{label}任务未失败", item["status"] != "failed", str(item["error"])[:120])
        check(f"{label}产出 token 用量", item["cost"]["total_tokens"] > 0)
        check(f"{label}成本可核算", item["cost"]["amount"] > 0)
        check(f"{label}无未计价模型", not item["cost"]["unpriced_models"], str(item["cost"]["unpriced_models"]))
        model_used = {row["agent"]: row for row in item["agents"]}
        report = model_used.get("report_agent")
        if report is not None:
            check(
                "报告 Agent 使用强模型",
                report["model"] == settings.agent_model_overrides.get("report_agent", {}).get(
                    "model", settings.llm_model
                ),
                str(report["model"]),
            )
        for row in item["agents"]:
            if row["status"] == "failed":
                check(f"{label} Agent {row['agent']} 未报错", False, str(row["error"])[:120])

    if second is not None:
        hits = second["cache_delta"].get("hits", 0)
        check("第 2 次检索命中缓存", hits > 0, f"hits={hits}")
        check(
            "第 2 次成本不高于第 1 次",
            second["cost"]["amount"] <= first["cost"]["amount"] + 1e-9,
            f"{second['cost']['amount']} vs {first['cost']['amount']}",
        )

    session = SessionLocal()
    try:
        summary = cost.cost_summary(session, _tenant_of(first["task_id"]), days=7)
    except Exception as exc:  # pragma: no cover - 汇总失败不影响主流程
        summary = {"error": str(exc)}
    finally:
        session.close()
    print(f"\n=== 成本汇总（近 7 天）===\n  {json.dumps(summary, ensure_ascii=False)[:600]}")

    if failures:
        print(f"\n验收失败 {len(failures)} 项: {failures}")
        return 1
    print("\n阶段六验收全部通过。")
    return 0


def _tenant_of(task_id: str) -> str:
    session = SessionLocal()
    try:
        task = session.get(AnalysisTask, task_id)
        return task.tenant_id if task is not None else "*"
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

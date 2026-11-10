"""把演示工单准备好（建单 + 可选跑分析），让工作台开箱可演示。

与 `seed` 的分工：`python -m app.db.seed` 只建工单（快，适合每次启动）；
本脚本额外**跑一遍 7 个 Agent 分析**（stub 下几秒完成），让详情抽屉一打开就有
建议、政策依据、风险等级与回复草稿。

幂等：已分析过的工单默认跳过（`--force` 可重跑），因此可以反复执行。

用法::

    python scripts/demo_tickets.py                # 建单 + 分析（跳过已分析的）
    python scripts/demo_tickets.py --force        # 全部重新分析
    python scripts/demo_tickets.py --list         # 只看现状，不改数据
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# 本脚本要"跑完就能看到建议"，所以强制同步执行（默认 threadpool 是异步的，
# 不等结果就查会全部显示"未产出建议"——踩过）。必须在 import app.* 之前设置。
os.environ.setdefault("TASK_RUN_INLINE", "true")

# 演示工单的前缀：只处理清单里的 TK-*，不碰评测/冒烟造的测试工单
DEMO_PREFIX = "TK-"
# 测试造出来的工单前缀（--prune 用）：这些是评测与冒烟留下的，演示前应清掉
TEST_PREFIXES = ("EVAL-", "E2E-", "DEBUG-", "REPRO-", "WF-TEST-", "CS-TEST-", "OBS-", "POL-TEST-")


def _prune_test_tickets(session, tenant) -> int:
    """清掉评测/冒烟造的工单（含其消息、事实、建议与分析任务）。

    只用于**开发库**演示前清理：这些工单是自动化测试留下的（前缀可辨认），
    留着会让工作台看起来像测试环境。真实环境不会执行本函数的功能。
    """
    from app.models.ticket import OrderFact, Recommendation, Ticket, TicketMessage
    from app.models.workflow import (
        AgentRun,
        AnalysisEvent,
        AnalysisTask,
        Approval,
        EvidenceCitation,
        Report,
        ToolCall,
    )

    rows = session.query(Ticket).filter(Ticket.tenant_id == tenant.id).all()
    targets = [
        row
        for row in rows
        if any(str(row.external_id or "").startswith(prefix) for prefix in TEST_PREFIXES)
    ]
    task_ids: list[str] = []
    for ticket in targets:
        if ticket.last_analysis_task_id:
            task_ids.append(ticket.last_analysis_task_id)
        for task in session.query(AnalysisTask).all():
            if (task.filters or {}).get("ticket_id") == ticket.id:
                task_ids.append(task.id)
    for task_id in set(task_ids):
        for model in (AgentRun, AnalysisEvent, ToolCall, EvidenceCitation, Approval, Report):
            session.query(model).filter(model.task_id == task_id).delete()
        task = session.get(AnalysisTask, task_id)
        if task is not None:
            session.delete(task)
    for ticket in targets:
        for model in (OrderFact, TicketMessage, Recommendation):
            session.query(model).filter(model.ticket_id == ticket.id).delete()
        session.delete(ticket)
    session.commit()
    return len(targets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备售后工单演示数据")
    parser.add_argument("--force", action="store_true", help="已分析的工单也重新分析")
    parser.add_argument("--list", action="store_true", help="只列出现状")
    parser.add_argument("--prune", action="store_true", help="清掉评测/冒烟造的测试工单（仅开发库）")
    parser.add_argument(
        "--allow-real-models",
        action="store_true",
        help="确认在已配置真实模型时也跑分析（6 张工单约 ¥0.2 起，会产生实际费用）",
    )
    parser.add_argument("--tenant", default="demo")
    args = parser.parse_args(argv)

    from app.config import get_settings
    from app.db.seed import seed_all
    from app.db.session import SessionLocal
    from app.models.identity import Tenant, User
    from app.models.ticket import OrderFact, Recommendation, Ticket, TicketMessage
    from app.services.task_dispatch import resolve_backend
    from app.services.ticket_analysis import FactIncomplete, start_analysis
    from app.services.ticket_service import TicketService

    settings = get_settings()
    backend = resolve_backend(settings)
    print("=== 环境 ===")
    print(
        f"  库={settings.database_url.split('://')[0]}  模型={settings.llm_provider}  执行={backend}"
    )
    if backend != "inline":
        # 脚本必须同步跑完才能报告结果；异步后端下它只会建单、随后立刻查询，
        # 于是全部显示「未产出建议」——这正是第一版踩过的坑，所以这里直接停下来说清楚。
        print("!! 执行后端不是 inline：脚本会边跑边查，必然误报「未产出建议」。")
        print("   请确认环境变量 TASK_RUN_INLINE 没有被别处覆盖成 false 后重跑。")
        return 2
    using_real_model = settings.llm_provider != "stub" and bool(settings.llm_api_key)
    if using_real_model and not args.list and not args.allow_real_models:
        # 与 run_evaluation 同样的纪律：可能花钱的操作必须显式确认。
        # （第一版没这道闸门，结果在真实模型下跑了 6 张工单 —— 这行注释就是那次教训。）
        print("!! 当前配置会调用真实模型，跑 6 张工单分析会产生实际费用（约 ¥0.2 起）。")
        print("   想要免费的演示数据：先在 backend/.env 设 LLM_PROVIDER=stub，")
        print("   或确认愿意花钱后加 --allow-real-models 重跑。")
        return 2

    session = SessionLocal()
    try:
        tenant = session.query(Tenant).filter(Tenant.code == args.tenant).first()
        if tenant is None:
            print(f"未找到租户 {args.tenant}，先执行：python -m app.db.seed")
            return 1

        if args.prune:
            removed = _prune_test_tickets(session, tenant)
            print(f"  已清理测试工单 {removed} 张（前缀 {', '.join(TEST_PREFIXES)}）")

        summary = seed_all(session) if not args.list else {}
        if summary.get("tickets"):
            print(f"  新建演示工单：{summary['tickets']}")

        user = session.query(User).filter(User.username == "ops").first()
        service = TicketService(session)
        tickets = (
            session.query(Ticket)
            .filter(Ticket.tenant_id == tenant.id)
            .filter(Ticket.external_id.like(f"{DEMO_PREFIX}%"))
            .order_by(Ticket.external_id.asc())
            .all()
        )
        print(f"\n=== 演示工单（{len(tickets)} 张）===")
        for ticket in tickets:
            recommendation = service.latest_recommendation(ticket.id)
            if args.list:
                print(
                    f"  {ticket.external_id}  {ticket.status:<18} "
                    f"{'有建议' if recommendation else '未分析'}  {ticket.subject[:26]}"
                )
                continue

            if recommendation is not None and ticket.status not in ("new", "analyzing") and not args.force:
                print(
                    f"  {ticket.external_id}  已分析（{recommendation.action_type} / "
                    f"{recommendation.risk_tier} / {recommendation.status}），跳过"
                )
                continue

            try:
                start_analysis(session, ticket=ticket, user=user)
            except FactIncomplete as exc:
                print(f"  {ticket.external_id}  拒绝分析（缺信息）：{exc.missing}")
                continue
            session.expire_all()
            ticket = session.get(Ticket, ticket.id)
            recommendation = service.latest_recommendation(ticket.id)
            if recommendation is None:
                print(f"  {ticket.external_id}  分析未产出建议（status={ticket.status}）")
                continue
            print(
                f"  {ticket.external_id}  {recommendation.action_type:<18} "
                f"{recommendation.risk_tier:<3} {recommendation.approval_requirement:<14} "
                f"{recommendation.status:<18} {ticket.subject[:22]}"
            )
    finally:
        session.close()

    print(
        "\n下一步：打开工作台 → 侧栏「售后工单」→（ops/ops123 登录）逐条点开查看；"
        "\n           待审批的 L3 建议用 reviewer/reviewer123 点「主管审批」。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

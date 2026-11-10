"""准备售后工作台无指导测试所需的演示工单与任务卡。

该脚本只处理演示租户和 ``TK-`` 工单，不删除任何数据。默认只检查现状；
加 ``--analyze`` 时会复用 ``demo_tickets.py``，在 stub 模型下生成可直接操作的建议。
脚本输出的是主持人任务卡，不是用户测试结果，结果仍需由业务方现场记录。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("TASK_RUN_INLINE", "true")

TASKS = [
    ("A", "TK-260924-0186", "找到下一步", "客户遇到什么问题、下一步是什么、是否已经超时？"),
    ("B", "TK-260924-0184", "判断是否需要主管", "这张退货退款工单能否直接发给客户？下一步应该怎么做？"),
    ("C", "TK-260924-0181", "准备客户回复", "使用常用回复作为起点，修改成准备发送的内容，但先不要发送。"),
    ("D", "TK-260924-0186", "内部协作", "留一条客户不可见备注并 @ 售后主管，请他关注这张工单。"),
    ("E", None, "主管维护话术", "主管新增一条物流问题常用回复并停用；客服账号不应看到维护入口。"),
]


DEFAULT_TEST_URL = "http://127.0.0.1:8000/#/login"


def _login_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    if not value:
        value = DEFAULT_TEST_URL
    if "#" not in value:
        value += "/#/login"
    return value


def build_test_payload(tenant_code: str, tickets: dict, *, base_url: str = DEFAULT_TEST_URL) -> dict:
    """Build an identifier-light preparation artifact, never a usability result."""
    tasks = []
    for code, external_id, title, prompt in TASKS:
        ticket = tickets.get(external_id) if external_id else None
        tasks.append(
            {
                "code": code,
                "ticket_id": external_id,
                "title": title,
                "prompt": prompt,
                "ticket_available": bool(ticket) if external_id else None,
                "ticket_analyzed": bool(ticket and ticket.last_analysis_task_id),
            }
        )
    return {
        "schema_version": "after-sales-usability-prep-v1",
        "data_kind": "demo_preparation_only",
        "tenant": tenant_code,
        "test_url": _login_url(base_url),
        "participants": [
            {"role": "客服", "username": "ops"},
            {"role": "主管/质检", "username": "reviewer"},
        ],
        "tasks": tasks,
        "not_a_user_test_result": True,
    }


def _write_json(path: str | None, payload: dict) -> None:
    if not path:
        return
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON 准备证据={output}")


def _run_analysis(tenant: str, force: bool) -> int:
    from demo_tickets import main as demo_main

    args = ["--tenant", tenant]
    if force:
        args.append("--force")
    return demo_main(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备售后客服无指导测试任务包")
    parser.add_argument("--tenant", default="demo", help="演示租户编码")
    parser.add_argument("--analyze", action="store_true", help="用当前配置跑演示工单分析")
    parser.add_argument("--force", action="store_true", help="配合 --analyze 强制重新分析")
    parser.add_argument("--json-out", help="输出脱敏的测试准备 JSON，不含密码、客户信息或测试结论")
    parser.add_argument(
        "--base-url",
        default=os.getenv("USABILITY_TEST_BASE_URL", DEFAULT_TEST_URL),
        help="参与者访问的前端地址（默认 http://127.0.0.1:8000）",
    )
    args = parser.parse_args(argv)

    if args.analyze and _run_analysis(args.tenant, args.force) != 0:
        return 2

    from app.db.seed import init_db
    from app.db.session import SessionLocal
    from app.models.identity import Tenant
    from app.models.ticket import Ticket

    # The script is also used against an older local SQLite file.  Run the
    # existing idempotent initializer so newly added ticket summary columns
    # are present before SQLAlchemy builds the query.
    init_db()
    session = SessionLocal()
    try:
        tenant = session.query(Tenant).filter(Tenant.code == args.tenant).first()
        if tenant is None:
            print(f"未找到租户 {args.tenant}。请先运行：python -m app.db.seed")
            return 1
        tickets = {
            row.external_id: row
            for row in session.query(Ticket)
            .filter(Ticket.tenant_id == tenant.id, Ticket.external_id.like("TK-%"))
            .all()
        }
        required = {external_id for _, external_id, _, _ in TASKS if external_id}
        missing = sorted(required - tickets.keys())
        if missing:
            print("缺少演示工单：" + ", ".join(missing))
            print("请先运行：python -m app.db.seed")
            return 1

        payload = build_test_payload(args.tenant, tickets, base_url=args.base_url)
        _write_json(args.json_out, payload)

        print("=== 售后工作台无指导测试准备结果 ===")
        print("数据性质：本地演示数据；订单、物流、政策事实均可能是 stub/mock。")
        print("参与者：客服使用 ops/ops123，主管或质检使用 reviewer/reviewer123。")
        print(f"测试地址：{payload['test_url']}")
        print("\n任务卡（主持人只读任务，不提示按钮位置）：")
        for code, external_id, title, prompt in TASKS:
            ticket = tickets.get(external_id) if external_id else None
            suffix = f"工单 {external_id} · {ticket.subject}" if ticket else "主管维护入口"
            status = "已分析" if ticket and ticket.last_analysis_task_id else "未分析"
            print(f"  {code}. {title}（{suffix}，{status}）")
            print(f"     {prompt}")
        print("\n记录模板：docs/product-redesign/客服可用性测试记录模板.md")
        print("注意：脚本只证明测试材料已准备，不代表任何用户测试指标已经达标。")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

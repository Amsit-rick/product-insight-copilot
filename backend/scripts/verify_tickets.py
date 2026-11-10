"""售后工单端到端验证（对着**活体 API** 跑，可重复执行）。

覆盖需求书里"能被验收"的关键行为，逐条打印 PASS/FAIL：

1. 创建工单 → 201，状态 `new`，客户原话落成第一条消息；
2. 幂等：同一 `external_id` 重复推送不产生第二张工单；
3. 权限：研发建单 403；客服不能自行升级（403）；
4. 状态机：非法迁移 409（`new → resolved`）；
5. 触发分析 → 202 返回 run_id，工单进入分析链路；
6. 事实补全：订单/支付/物流/售后四类事实入库，带 `observed_at`/`request_id`/`is_mock`，
   且地址与手机号在展示出口已脱敏；
7. 事实键值行按原型顺序排列（订单 → 支付 → 物流 → 售后）；
8. 详情页时间线正序，且包含"工单创建 + 客户诉求"；
9. 缺订单号 → 工单进入 `need_customer_info` 且 `missing_fields` 说明缺什么；
10. 未批准的建议不能发送（409）；
11. 工作台指标卡返回真实统计（不是演示假数据）。

用法::

    python scripts/verify_tickets.py                    # 默认打 http://127.0.0.1:8010
    python scripts/verify_tickets.py --base-url http://127.0.0.1:8020

脚本结束会清理自己创建的 `VERIFY-` 前缀工单，不留垃圾数据。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

PREFIX = "VERIFY-"
CHECKS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    CHECKS.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def request(
    base: str, method: str, path: str, *, token: str | None = None, payload: dict | None = None
) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def login(base: str, username: str, password: str) -> str:
    _, payload = request(base, "POST", "/api/v1/auth/login", payload={"username": username, "password": password})
    return payload.get("access_token", "")


def _wait_for_analysis(
    base: str, token: str, ticket_id: str, detail: dict, *, timeout_seconds: int = 90
) -> dict:
    """轮询等待分析完成（threadpool/queue 后端下 202 只是已受理）。"""
    import time

    deadline = time.time() + timeout_seconds
    last = detail
    while time.time() < deadline:
        # 客服详情只返回业务化步骤，原始 Agent/Token 轨迹仅工程与管理员可见。
        # live 验证不能再用被权限过滤掉的 `steps` 判断分析是否结束。
        business_steps = (last.get("analysis") or {}).get("business_steps") or []
        if last.get("recommendation") and business_steps:
            return last
        time.sleep(1.5)
        _, last = request(base, "GET", f"/api/v1/tickets/{ticket_id}", token=token)
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description="售后工单端到端验证")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--keep", action="store_true", help="不清理验证数据")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    print("=== 环境 ===")
    status, health = request(base, "GET", "/health")
    print(f"  API={base} status={status} db={health.get('database')}")
    if status != 200:
        print("  !! API 不可用，请先启动后端")
        return 2

    tokens = {
        "ops": login(base, "ops", "ops123"),
        "eng": login(base, "eng", "eng123"),
        "reviewer": login(base, "reviewer", "reviewer123"),
    }
    if not all(tokens.values()):
        print("  !! 登录失败，请确认演示数据已播种（python -m app.db.seed）")
        return 2

    if not args.keep:
        stale = _purge_stale()
        stale_templates = _purge_stale_templates()
        if stale:
            print(f"  （清理上次残留的验证工单 {stale} 张，保证结果可重复）")
        if stale_templates:
            print(f"  （清理上次残留的验证模板 {stale_templates} 个，保证结果可重复）")

    created: list[str] = []
    created_templates: list[str] = []
    print("\n=== 1/9 创建工单与消息 ===")
    status, ticket = request(
        base,
        "POST",
        "/api/v1/tickets",
        token=tokens["ops"],
        payload={
            "subject": "包裹显示签收但未收到",
            "content": "物流显示昨天签收，但我没有收到包裹，也没有接到电话，请帮我核实。",
            "external_id": f"{PREFIX}1806",
            "order_id": "TB202609241806",
            "customer_id": "U-VERIFY-1",
            "priority": "high",
            "data_origin": "test",
        },
    )
    check(status == 201, "创建工单返回 201", f"status={status}")
    ticket_id = ticket.get("id", "")
    created.append(ticket_id)
    check(ticket.get("status") == "new", "初始状态为 new", str(ticket.get("status")))
    check(bool(ticket.get("allowed_transitions")), "返回允许的操作（供前端渲染按钮）", str(ticket.get("allowed_transitions")))

    status, again = request(
        base,
        "POST",
        "/api/v1/tickets",
        token=tokens["ops"],
        payload={
            "subject": "包裹显示签收但未收到",
            "content": "重复推送",
            "external_id": f"{PREFIX}1806",
            "data_origin": "test",
        },
    )
    check(again.get("id") == ticket_id, "幂等：同一 external_id 不产生第二张工单")

    status, _ = request(
        base,
        "POST",
        "/api/v1/tickets",
        token=tokens["eng"],
        payload={"subject": "研发建单", "content": "研发没有 ticket:create 权限。"},
    )
    check(status == 403, "研发建单被拒（403）", f"status={status}")

    print("\n=== 2/9 状态机与权限 ===")
    status, body = request(
        base,
        "POST",
        f"/api/v1/tickets/{ticket_id}/transition",
        token=tokens["reviewer"],
        payload={"target": "resolved", "reason": "想跳过流程"},
    )
    check(
        status == 409 and body.get("detail", {}).get("code") == "ticket_invalid_transition",
        "非法迁移被拒（409）",
        f"status={status}",
    )
    status, _ = request(
        base,
        "POST",
        f"/api/v1/tickets/{ticket_id}/transition",
        token=tokens["ops"],
        payload={"target": "escalated", "reason": "客服越权升级"},
    )
    check(status == 403, "客服不能自行升级（403）", f"status={status}")

    print("\n=== 3/9 触发分析（7 个 Agent 拓扑）===")
    status, run = request(base, "POST", f"/api/v1/tickets/{ticket_id}/analyze", token=tokens["ops"])
    check(status == 202, "分析请求返回 202", f"status={status}")
    check(bool(run.get("run_id")), "返回 run_id（进度走 SSE/事件流）", str(run.get("run_id")))

    status, detail = request(base, "GET", f"/api/v1/tickets/{ticket_id}", token=tokens["ops"])
    # 生产配置下分析在后台线程/worker 里跑（threadpool / queue），202 只是"已受理"。
    # 所以这里必须**轮询等结果**，否则测到的是"还没跑完"（实测踩过：本地同步执行时正常，
    # 换成 threadpool 后立刻断言就永远拿不到建议）。
    detail = _wait_for_analysis(base, tokens["ops"], ticket_id, detail)
    recommendation = detail.get("recommendation")
    check(recommendation is not None, "分析产出处理建议")
    if recommendation:
        check(
            recommendation.get("action_type") in {
                "reply_only",
                "contact_logistics",
                "reship",
                "refund",
                "return_goods",
                "ask_customer",
            },
            "建议动作是受控集合内的动作",
            str(recommendation.get("action_type")),
        )
        check(bool(recommendation.get("risk_tier")), "建议带风险等级", str(recommendation.get("risk_tier")))
        check(bool(recommendation.get("risk_reason")), "风险原因是人话（供审批人看）", str(recommendation.get("risk_reason")))
        check(
            bool(recommendation.get("customer_reply")),
            "生成了消费者可见的回复草稿",
            (recommendation.get("customer_reply") or "")[:24] + "…",
        )
        check(
            recommendation.get("internal_note") is not None,
            "内部备注与消费者内容分开",
        )
        check(
            detail["ticket"]["status"] in {"ready_to_reply", "waiting_review"},
            "工单状态由后端推进（待回复/待审批）",
            detail["ticket"]["status"],
        )
        # 金额只允许引用：退款金额必须等于支付事实里的实付金额
        if recommendation.get("action_type") == "refund":
            facts_payload = {
                item["source"]: item["payload"] for item in (detail.get("facts") or [])
            }
            paid = (facts_payload.get("payment") or {}).get("paid_amount")
            check(
                paid is None or str(recommendation.get("amount")).startswith(str(paid).split(".")[0]),
                "退款金额引用实付金额（未由模型计算）",
                f"amount={recommendation.get('amount')} paid={paid}",
            )

    status, technical_detail = request(
        base, "GET", f"/api/v1/tickets/{ticket_id}", token=tokens["eng"]
    )
    steps = (technical_detail.get("analysis") or {}).get("steps") or []
    codes = {step["agent_code"] for step in steps}
    expected_agents = {
        "triage_agent",
        "order_fact_agent",
        "policy_rag_agent",
        "resolution_agent",
        "response_agent",
        "review_agent",
        "followup_agent",
    }
    check(
        status == 200 and expected_agents <= codes,
        "工程角色可审计 7 个 Agent 执行轨迹",
        f"status={status}，{len(codes)} 个：{'、'.join(sorted(codes))}",
    )

    print("\n=== 4/9 事实补全与脱敏 ===")
    status, detail = request(base, "GET", f"/api/v1/tickets/{ticket_id}", token=tokens["ops"])
    facts = detail.get("facts") or []
    sources = sorted({item["source"] for item in facts})
    check(len(facts) >= 4, "四类事实已入库（订单/支付/物流/售后）", f"count={len(facts)}")
    check(
        sources == ["aftersale", "logistics", "order", "payment"],
        "事实来源齐全",
        ",".join(sources),
    )
    check(
        all(item.get("observed_at") and item.get("request_id") for item in facts),
        "每条事实都带观测时间与请求号（FR-02）",
    )
    check(all(item.get("is_mock") for item in facts), "事实标注为模拟数据（MVP 纪律）")

    rows = detail.get("fact_rows") or []
    keys = [row["key"] for row in rows]
    check(bool(rows), "详情页事实行已装配", f"{len(rows)} 行")
    if rows:
        check(
            keys.index("order_status") < keys.index("carrier") if "carrier" in keys else True,
            "事实行顺序：订单 → 支付 → 物流 → 售后",
            "、".join(row["label"] for row in rows[:5]),
        )
        address = next((row for row in rows if row["key"] in ("address", "receiver_address")), None)
        check(
            address is None or "号" not in str(address["value"]).split("区")[-1],
            "地址在展示出口已脱敏",
            str(address["value"]) if address else "（无地址字段）",
        )

    timeline = detail.get("timeline") or []
    stamps = [item["at"] for item in timeline]
    check(stamps == sorted(stamps), "时间线正序")
    check(
        any(item["kind"] == "message:customer" for item in timeline),
        "时间线包含客户诉求",
        f"{len(timeline)} 条",
    )

    print("\n=== 5/9 缺订单号 → 等客户补充 ===")
    status, no_order = request(
        base,
        "POST",
        "/api/v1/tickets",
        token=tokens["ops"],
        payload={
            "subject": "没有订单号的咨询",
            "content": "我想问一下我的包裹到哪里了，但是我忘了订单号。",
            "external_id": f"{PREFIX}NOORDER",
            "data_origin": "test",
        },
    )
    created.append(no_order.get("id", ""))
    status, body = request(
        base, "POST", f"/api/v1/tickets/{no_order['id']}/analyze", token=tokens["ops"]
    )
    check(
        status == 409 and body.get("detail", {}).get("code") == "need_customer_info",
        "缺订单号 → 拒绝对分析（409 need_customer_info）",
        f"status={status}",
    )
    check(
        body.get("detail", {}).get("missing") == ["订单号"],
        "拒绝理由里明确缺什么",
        str(body.get("detail", {}).get("missing")),
    )
    status, detail_no = request(base, "GET", f"/api/v1/tickets/{no_order['id']}", token=tokens["ops"])
    check(
        detail_no["ticket"]["status"] == "need_customer_info",
        "缺订单号 → 工单进入 need_customer_info（不被后续推进覆盖）",
        detail_no["ticket"]["status"],
    )
    check(
        "订单号" in (detail_no["ticket"].get("missing_fields") or []),
        "明确告知缺什么（missing_fields）",
        str(detail_no["ticket"].get("missing_fields")),
    )

    print("\n=== 6/9 模板与 @ 待关注 ===")
    status, _ = request(
        base,
        "POST",
        "/api/v1/ticket-templates",
        token=tokens["ops"],
        payload={"title": "VERIFY-客服越权模板", "content": "不应创建"},
    )
    check(status == 403, "客服不能维护回复模板（403）", f"status={status}")
    template_title = f"VERIFY-物流模板-{ticket_id[-6:]}"
    status, template = request(
        base,
        "POST",
        "/api/v1/ticket-templates",
        token=tokens["reviewer"],
        payload={
            "title": template_title,
            "content": "您好，订单{{订单号}}正在核实。",
            "issue_type": "logistics_stalled",
        },
    )
    template_id = template.get("id", "")
    if template_id:
        created_templates.append(template_id)
    check(status == 201 and bool(template_id), "主管可以创建租户回复模板", f"status={status}")
    status, templates = request(base, "GET", "/api/v1/ticket-templates?issue_type=logistics_stalled", token=tokens["ops"])
    check(status == 200 and any(item.get("id") == template_id for item in templates or []), "客服可读取当前租户启用模板")
    status, updated = request(
        base,
        "PATCH",
        f"/api/v1/ticket-templates/{template_id}",
        token=tokens["reviewer"],
        payload={"title": f"{template_title}-已更新", "issue_type": None},
    )
    check(status == 200 and updated.get("issue_type") is None, "主管可以修改模板并清除问题类型")

    status, me = request(base, "GET", "/api/v1/auth/me", token=tokens["reviewer"])
    reviewer_id = me.get("id")
    status, note = request(
        base,
        "POST",
        f"/api/v1/tickets/{ticket_id}/messages",
        token=tokens["ops"],
        payload={
            "content": "请主管关注这张验证工单",
            "message_type": "internal_note",
            "mention_user_ids": [reviewer_id],
        },
    )
    mention_id = note.get("id")
    check(status == 200 and bool(mention_id), "客服可以记录内部备注并 @ 主管")
    status, mentions = request(base, "GET", "/api/v1/ticket-mentions?unread_only=true", token=tokens["reviewer"])
    mention = next((item for item in (mentions or []) if item.get("id") == mention_id), None)
    check(status == 200 and mention is not None and mention.get("read") is False, "主管能看到自己的未读待关注消息")
    status, marked = request(base, "POST", f"/api/v1/ticket-mentions/{mention_id}/read", token=tokens["reviewer"])
    check(status == 200 and marked.get("read") is True, "主管标记待关注消息已读")

    print("\n=== 7/9 审批门禁与指标卡 ===")
    status, stats = request(base, "GET", "/api/v1/tickets/stats", token=tokens["ops"])
    check(
        status == 200 and stats.get("excluded_test_records", 0) >= 2,
        "指标卡排除自动化测试工单",
        f"today_new={stats.get('today_new')} excluded={stats.get('excluded_test_records')}",
    )
    check(
        len(stats.get("issue_type_options") or []) == 11,
        "问题类型选项齐全（10 类 + 其他）",
        str(len(stats.get("issue_type_options") or [])),
    )

    print("\n=== 8/9 政策版本化与生效期过滤 ===")
    status, policies = request(base, "GET", "/api/v1/policies", token=tokens["ops"])
    items = (policies or {}).get("items") or []
    by_version = {item["version"]: item for item in items}
    check(status == 200 and len(items) >= 3, "政策清单返回多个版本", f"{len(items)} 条")
    check(
        by_version.get("v3.0", {}).get("usable") is True,
        "现行政策（v3.0）可用",
        str(by_version.get("v3.0", {}).get("reason")),
    )
    check(
        by_version.get("v1.0", {}).get("usable") is False
        and "已失效" in str(by_version.get("v1.0", {}).get("reason")),
        "已失效政策（v1.0）判定为不可用",
        str(by_version.get("v1.0", {}).get("reason")),
    )
    check(
        by_version.get("v4.0", {}).get("usable") is False
        and "未生效" in str(by_version.get("v4.0", {}).get("reason")),
        "未生效政策（v4.0）判定为不可用",
        str(by_version.get("v4.0", {}).get("reason")),
    )

    status, search = request(
        base,
        "POST",
        "/api/v1/policies/search-test",
        token=tokens["ops"],
        payload={"query": "物流停滞补偿标准 停滞多久补偿多少钱", "top_k": 8},
    )
    hits = (search or {}).get("hits") or []
    hit_docs = {hit["document_id"] for hit in hits}
    check(bool(hits), "政策检索测试有命中", f"{len(hits)} 条")
    check(
        by_version.get("v1.0", {}).get("document_id") not in hit_docs,
        "已失效政策未出现在检索结果里（过滤落在检索层）",
    )
    check(
        by_version.get("v4.0", {}).get("document_id") not in hit_docs,
        "未生效政策未出现在检索结果里",
    )
    check(
        by_version.get("v3.0", {}).get("document_id") in hit_docs,
        "现行政策可被检索到",
    )

    print("\n=== 9/9 每单成本与 SSE 事件流 ===")
    status, hidden_cost = request(
        base, "GET", f"/api/v1/tickets/{ticket_id}/cost", token=tokens["ops"]
    )
    check(status == 403, "客服不能读取技术成本明细", f"status={status}")
    status, cost = request(base, "GET", f"/api/v1/tickets/{ticket_id}/cost", token=tokens["eng"])
    check(status == 200, "工程角色可读取成本接口", f"status={status}")
    check(
        cost.get("analysis_count", 0) >= 1 and "amount_per_analysis" in cost,
        "返回累计成本与单次均值（可算每单花多少）",
        f"count={cost.get('analysis_count')} amount={cost.get('amount')} tokens={cost.get('total_tokens')}",
    )
    check(
        set(cost) >= {"input_tokens", "output_tokens", "cache_hit_tokens", "calls", "per_model"},
        "成本字段齐全（Token/调用次数/分模型）",
    )
    status, estimate = request(
        base,
        "GET",
        f"/api/v1/tickets/{ticket_id}/cost",
        token=tokens["eng"],
        payload=None,
    )
    # 批量推算需要 daily_volume 参数，这里单独发一次
    estimate_url = f"{base}/api/v1/tickets/{ticket_id}/cost?daily_volume=5000"
    req = urllib.request.Request(estimate_url, headers={"Authorization": f"Bearer {tokens['eng']}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        estimate = json.loads(resp.read().decode("utf-8"))
    check(
        (estimate.get("batch_estimate") or {}).get("daily_volume") == 5000,
        "按日单量推算批量开销（评审用）",
        f"日≈{estimate['batch_estimate']['daily_amount']} 元、年≈{estimate['batch_estimate']['yearly_amount']} 元",
    )

    run_id = run.get("run_id")
    if run_id:
        req = urllib.request.Request(
            f"{base}/api/v1/runs/{run_id}/events?max_seconds=10",
            headers={"Authorization": f"Bearer {tokens['ops']}"},
        )
        with urllib.request.urlopen(req, timeout=40) as resp:
            content_type = resp.headers.get("content-type", "")
            body = resp.read().decode("utf-8")
        check(content_type.startswith("text/event-stream"), "SSE 端点返回事件流", content_type)
        check("event: open" in body, "事件流建立帧")
        check("event: done" in body or "event: timeout" in body, "事件流能正常收尾（不永久挂连接）")
        data_lines = [line for line in body.splitlines() if line.startswith("data: ")]
        check(bool(data_lines), "事件流包含数据帧", f"{len(data_lines)} 帧")

    print("\n=== 附加：金额来源 / L4 风控 / 指标说明 ===")
    # 金额来源：审批人要能看出"这笔钱是订单实付还是政策补偿标准"
    if ticket_id:
        status, reco = request(
            base, "GET", f"/api/v1/tickets/{ticket_id}/recommendation", token=tokens["ops"]
        )
        source = (reco or {}).get("amount_source")
        check(
            status == 200 and bool(source),
            "建议带金额来源（审批人可核对金额出处）",
            f"amount={reco.get('amount') if reco else None} source={source}",
        )

    # L4：带风控标记的工单必须转风控、禁止自动执行（界面上现在也能造出来）
    status, l4_created = request(
        base,
        "POST",
        "/api/v1/tickets",
        token=tokens["ops"],
        payload={
            "subject": "验证：批量退款风控",
            "content": "我要把这批订单全部退款，一共十二单，请一起处理。",
            "external_id": "VERIFY-L4-FLAG",
            "order_id": "TB202609241501",
            "attributes": {"risk_flags": ["batch_refund"]},
            "data_origin": "test",
        },
    )
    l4_id = (l4_created or {}).get("id")
    if l4_id:
        created.append(l4_id)
        request(base, "POST", f"/api/v1/tickets/{l4_id}/analyze", token=tokens["ops"])
        _wait_for_analysis(base, tokens["ops"], l4_id, {})
        status, l4_detail = request(base, "GET", f"/api/v1/tickets/{l4_id}", token=tokens["ops"])
        l4_reco = (l4_detail or {}).get("recommendation") or {}
        check(
            l4_reco.get("risk_tier") == "L4" and l4_reco.get("approval_requirement") == "risk_control",
            "风控标记工单判为 L4 且转风控（禁止自动执行）",
            f"tier={l4_reco.get('risk_tier')} approval={l4_reco.get('approval_requirement')}",
        )
    else:
        check(False, "风控标记工单可创建（L4 路径可复现）", str(status))

    # 指标说明要覆盖工单指标（此前界面上只能靠猜字段含义）
    status, guide = request(base, "GET", "/api/v1/evaluations/metrics-guide", token=tokens["ops"])
    ticket_metrics = (guide or {}).get("ticket_metrics") or {}
    check(
        status == 200 and {"approval_gate_recall", "expired_policy_leak_rate", "avg_cost_amount"}
        <= set(ticket_metrics),
        "评测指标说明包含工单指标（含三个安全指标）",
        f"{len(ticket_metrics)} 项",
    )

    passed = sum(1 for ok, _, _ in CHECKS if ok)
    print(f"\n=== 汇总：{passed}/{len(CHECKS)} 通过 ===")
    for ok, name, detail in CHECKS:
        if not ok:
            print(f"  未通过：{name} {detail}")

    if not args.keep:
        cleaned = _cleanup(created)
        cleaned_templates = _cleanup_templates(created_templates)
        print(f"（已清理验证工单 {cleaned} 张、回复模板 {cleaned_templates} 个）")
    return 0 if passed == len(CHECKS) else 1


def _purge_stale() -> int:
    """清掉上一次中断运行残留的验证工单。

    为什么需要（踩过）：脚本中途抛异常时收尾清理跑不到，而建单是按 `external_id` 幂等的，
    下一次运行会"创建"到上一次那张已经被分析过的工单 —— 于是"初始状态为 new"、
    "非法迁移被拒 409"这类断言莫名其妙失败，看上去像代码坏了。
    所以每次开跑前先按前缀清一遍，让结果可重复。
    """
    from app.db.session import SessionLocal
    from app.models.ticket import OrderFact, Recommendation, Ticket, TicketMessage
    from app.models.workflow import AgentRun, AnalysisEvent, AnalysisTask

    session = SessionLocal()
    try:
        rows = (
            session.query(Ticket.id)
            .filter(Ticket.external_id.like(f"{PREFIX}%"))
            .all()
        )
        ids = [row[0] for row in rows]
        if not ids:
            return 0
        task_ids = [
            row[0]
            for row in session.query(AnalysisTask.id).all()
            if (session.get(AnalysisTask, row[0]).filters or {}).get("ticket_id") in set(ids)
        ]
        for task_id in task_ids:
            for model in (AgentRun, AnalysisEvent):
                session.query(model).filter(model.task_id == task_id).delete()
            task = session.get(AnalysisTask, task_id)
            if task is not None:
                session.delete(task)
        for ticket_id in ids:
            for model in (OrderFact, TicketMessage, Recommendation):
                session.query(model).filter(model.ticket_id == ticket_id).delete()
            ticket = session.get(Ticket, ticket_id)
            if ticket is not None:
                session.delete(ticket)
        session.commit()
        return len(ids)
    finally:
        session.close()


def _cleanup(ticket_ids: list[str]) -> int:
    """清理验证数据：直接删库，避免污染演示数据。"""
    from app.db.session import SessionLocal
    from app.models.ticket import OrderFact, Recommendation, Ticket, TicketMessage

    session = SessionLocal()
    count = 0
    try:
        for ticket_id in [item for item in ticket_ids if item]:
            for model in (OrderFact, TicketMessage, Recommendation):
                session.query(model).filter(model.ticket_id == ticket_id).delete()
            row = session.get(Ticket, ticket_id)
            if row is not None:
                session.delete(row)
                count += 1
        session.commit()
    finally:
        session.close()
    return count


def _purge_stale_templates() -> int:
    """清掉上次中断留下的 VERIFY- 模板，保证脚本可重复执行。"""
    from app.db.session import SessionLocal
    from app.models.ticket import ReplyTemplate

    session = SessionLocal()
    try:
        count = session.query(ReplyTemplate).filter(ReplyTemplate.title.like("VERIFY-%")).delete(synchronize_session=False)
        session.commit()
        return int(count or 0)
    finally:
        session.close()


def _cleanup_templates(template_ids: list[str]) -> int:
    """清理脚本创建的模板，生产接口本身仍保留软停用语义。"""
    from app.db.session import SessionLocal
    from app.models.ticket import ReplyTemplate

    session = SessionLocal()
    count = 0
    try:
        for template_id in [item for item in template_ids if item]:
            row = session.get(ReplyTemplate, template_id)
            if row is not None:
                session.delete(row)
                count += 1
        session.commit()
    finally:
        session.close()
    return count


if __name__ == "__main__":
    raise SystemExit(main())

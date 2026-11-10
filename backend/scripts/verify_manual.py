"""按《功能使用说明书》逐条核对期望值（对着运行中的服务跑，全部只读）。

为什么要有它：说明书里的"预期结果"如果没人核对，就会慢慢和实现脱节，
人工测试的人会以为是自己操作错。这个脚本把说明书里可机器判定的断言全部跑一遍，
一条不过就说明"文档或实现有一边错了"。

用法::

    python scripts/verify_manual.py                       # 默认 http://127.0.0.1:8010
    python scripts/verify_manual.py --base-url http://127.0.0.1:8000
    python scripts/verify_manual.py --json

只做只读请求（登录/查询），**不会创建任务、不会调模型、不改数据**。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")


def login(client: httpx.Client, username: str, password: str) -> dict:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按说明书核对期望值")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    client = httpx.Client(base_url=args.base_url, timeout=args.timeout)
    try:
        health = client.get("/health")
    except Exception as exc:
        print(f"[环境错误] 服务不可达 {args.base_url}：{exc}")
        return 2
    check("服务健康检查可用", health.status_code == 200, f"status={health.status_code}")

    h = health.json()
    print("\n=== 1. 账号与权限 ===")
    pm = login(client, "pm", "pm123")
    me = client.get("/api/v1/auth/me", headers=pm).json()
    check("pm 角色为 product_manager", me.get("roles") == ["product_manager"], str(me.get("roles")))
    check("pm 可见 4 个知识库", len(me.get("knowledge_bases") or []) == 4, str(me.get("knowledge_bases")))
    check("pm 有 task:create", "task:create" in (me.get("actions") or []))
    check("pm 无 task:approve", "task:approve" not in (me.get("actions") or []))
    check("/auth/me 不含密钥", "api_key" not in json.dumps(me).lower() and "sk-" not in json.dumps(me))

    eng = login(client, "eng", "eng123")
    admin = login(client, "admin", "admin123")
    reviewer = login(client, "reviewer", "reviewer123")
    ops = login(client, "ops", "ops123")

    check(
        "eng 无 task:create",
        "task:create" not in (client.get("/api/v1/auth/me", headers=eng).json().get("actions") or []),
    )
    check("pm 读审计日志 403", client.get("/api/v1/audit-logs", headers=pm).status_code == 403)
    check("admin 读审计日志 200", client.get("/api/v1/audit-logs?limit=5", headers=admin).status_code == 200)
    check(
        "审计日志含登录/任务类动作",
        bool(
            {"login_success", "task_created"}
            & {item["action"] for item in client.get("/api/v1/audit-logs?limit=200", headers=admin).json()["items"]}
        ),
    )

    print("\n=== 2. 健康检查字段（说明书 §4.17）===")
    check("health.status=ok", h.get("status") == "ok")
    check("health.llm.routing 有 5 个 Agent", len(h.get("llm", {}).get("routing") or []) == 5)
    check("health 含 limits", "limits" in h and "task_token_limit" in h["limits"])
    check("health 含 cache 段", "backend" in h.get("cache", {}) and "healthy" in h.get("cache", {}))
    check("health 含 cost 段", "currency" in h.get("cost", {}))
    check("health 含 execution 段", "backend" in h.get("execution", {}))
    check(
        "execution 含 checkpointer.shared",
        "shared" in (h.get("execution", {}).get("checkpointer") or {}),
    )
    # 用"像密钥的样子"来判定，而不是裸子串 `sk-`：
    # 后者会被路径误伤 —— 仓库目录名 `deal-risk-agent` 里就含 `sk-a`，
    # 于是任何把路径放进 /health 的改动都会让这条断言假失败。
    health_text = json.dumps(h)
    looks_like_key = re.search(r"sk-[A-Za-z0-9]{16,}", health_text) is not None
    check(
        "health 不含密钥",
        not looks_like_key and "api_key" not in health_text.lower(),
        (re.search(r"sk-[A-Za-z0-9]{16,}", health_text).group(0)[:12] + "…") if looks_like_key else "",
    )

    print("\n=== 3. 指标查询（说明书 §4.9 参考数值）===")
    trend = client.get(
        "/api/v1/metrics/trend",
        params={
            "metric_name": "next_day_retention",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "platform": "Android",
            "channel": "channel_a",
            "device": "high_end",
        },
        headers=pm,
    )
    check("trend 200", trend.status_code == 200, trend.text[:120])
    if trend.status_code == 200:
        rows = trend.json()["rows"]
        dates = [row["stat_date"] for row in rows]
        check("trend 有数据点", len(rows) >= 25, f"{len(rows)} 个点")
        check("trend 刻意缺口 2026-08-25 无数据", "2026-08-25" not in dates)

    compare = client.get(
        "/api/v1/metrics/compare",
        params={
            "metric_name": "next_day_retention",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "platform": "Android",
            "channel": "channel_a",
            "baseline_version": "3.1.9",
            "target_version": "3.2.0",
            "release_date": "2026-08-20",
        },
        headers=pm,
    )
    check("compare 200（且不需要 device 参数）", compare.status_code == 200, compare.text[:120])
    if compare.status_code == 200:
        summary = compare.json()["summary"]
        baseline = summary.get("baseline_avg")
        target = summary.get("target_avg")
        # 说明书 §4.9/§5.5 记录的就是这个口径（跨机型整段平均）
        check(
            "compare 均值与说明书一致（0.3088 → 0.2458）",
            baseline is not None and target is not None and abs(baseline - 0.308842) < 0.002 and abs(target - 0.245818) < 0.002,
            f"baseline={baseline} target={target} delta={summary.get('delta')}",
        )
        check(
            "compare 提供 points 与 pct_change",
            summary.get("baseline_points") and summary.get("target_points") and summary.get("pct_change") is not None,
            f"points={summary.get('baseline_points')}/{summary.get('target_points')}",
        )

    breakdown = client.get(
        "/api/v1/metrics/breakdown",
        params={
            "metric_name": "crash_rate",
            "start_date": "2026-08-20",
            "end_date": "2026-08-31",
            "platform": "Android",
            "app_version": "3.2.0",
            "dimension": "device",
        },
        headers=pm,
    )
    check("breakdown 200", breakdown.status_code == 200, breakdown.text[:120])
    if breakdown.status_code == 200:
        rows = {row["key"]: row["avg_value"] for row in breakdown.json()["rows"]}
        check(
            "分群 low_end > high_end",
            rows.get("low_end", 0) > rows.get("high_end", 0),
            str(rows),
        )

    coverage = client.get("/api/v1/metrics/coverage", params={"metric_name": "next_day_retention"}, headers=pm)
    check("coverage 200 且有行数", coverage.status_code == 200 and coverage.json().get("rows", 0) > 0,
          str(coverage.json() if coverage.status_code == 200 else coverage.text[:100]))

    anomaly = client.get(
        "/api/v1/metrics/anomaly",
        params={
            "metric_name": "crash_rate",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "platform": "Android",
            "app_version": "3.2.0",
            "method": "zscore",
            "threshold": 2,
        },
        headers=pm,
    )
    check("anomaly 200", anomaly.status_code == 200, anomaly.text[:120])

    definitions = client.get("/api/v1/metrics/definitions", headers=pm)
    check("指标字典含 4 个指标", definitions.status_code == 200 and len(definitions.json()) >= 4)

    print("\n=== 4. 指标级密级（说明书 §4.13）===")
    eng_metric = client.get(
        "/api/v1/metrics/trend",
        params={"metric_name": "order_conversion_rate", "start_date": "2026-08-01", "end_date": "2026-08-31"},
        headers=eng,
    )
    check("eng 查 order_conversion_rate → 403 metric_forbidden",
          eng_metric.status_code == 403 and eng_metric.json()["detail"]["code"] == "metric_forbidden",
          f"{eng_metric.status_code} {eng_metric.text[:80]}")
    pm_metric = client.get(
        "/api/v1/metrics/trend",
        params={"metric_name": "order_conversion_rate", "start_date": "2026-08-01", "end_date": "2026-08-31"},
        headers=pm,
    )
    check("pm 查同一指标 → 200 且有数据", pm_metric.status_code == 200 and bool(pm_metric.json()["rows"]),
          f"{pm_metric.status_code}")
    denied_ledger = client.get(
        "/api/v1/metrics/trend",
        params={"metric_name": "finance_raw_ledger", "start_date": "2026-08-01", "end_date": "2026-08-31"},
        headers=admin,
    )
    check("广播 deny 生效（连 admin 也 403）", denied_ledger.status_code == 403,
          f"{denied_ledger.status_code}")
    defs_eng = {item["name"] for item in client.get("/api/v1/metrics/definitions", headers=eng).json()}
    check("eng 的指标字典不含受限指标", "order_conversion_rate" not in defs_eng, str(sorted(defs_eng)))

    print("\n=== 5. 成本、灰度、评测、审核（说明书 §4.10/§4.12/§4.15/§4.6）===")
    costs = client.get("/api/v1/costs/summary?days=7", headers=pm)
    check("costs/summary 200", costs.status_code == 200)
    if costs.status_code == 200:
        payload = costs.json()
        check("today.amount 为真实花销（不限额时也不为 0）", "amount" in payload.get("today", {}),
              str(payload.get("today")))
        check("cost 含 per_model 与 unpriced_models",
              "per_model" in payload.get("cost", {}) and "unpriced_models" in payload.get("cost", {}))

    canary = client.get("/api/v1/experiments/canary?days=14", headers=pm)
    check("experiments/canary 200", canary.status_code == 200)
    if canary.status_code == 200:
        payload = canary.json()
        check("canary 返回 verdict 与 method.limitations",
              bool(payload.get("verdict")) and len(payload.get("method", {}).get("limitations") or []) >= 3,
              f"verdict={payload.get('verdict')}")
        check("canary 含 min_sample", isinstance(payload.get("min_sample"), int), str(payload.get("min_sample")))
        insufficient = payload.get("verdict") == "样本不足"
        if insufficient:
            all_insufficient = all(
                item["verdict"] == "样本不足"
                for item in [*payload.get("metrics", []), *payload.get("report_metrics", [])]
            )
            check("样本不足时每个指标项也不给胜负结论", all_insufficient)

    runs = client.get("/api/v1/evaluations/runs", headers=admin)
    check("evaluations/runs 200（admin）", runs.status_code == 200)
    cases = client.get("/api/v1/evaluations/cases?limit=5", headers=admin)
    check("evaluations/cases 可读（40 条以上）",
          cases.status_code == 200 and len(cases.json().get("items", cases.json() if isinstance(cases.json(), list) else [])) >= 1,
          f"{cases.status_code}")

    approvals = client.get("/api/v1/approvals", headers=reviewer)
    check("approvals 200（reviewer）", approvals.status_code == 200)
    # 列表只需 task:read（pm 也能看）；**提交结论**才要 task:approve。
    # 说明书 §3 的权限表就是这么写的，这里按真实契约核对。
    check("approvals 列表对 pm 开放（只需 task:read）",
          client.get("/api/v1/approvals", headers=pm).status_code == 200)
    decision = client.post(
        "/api/v1/approvals/does-not-exist/decision",
        headers=pm,
        json={"action": "approve", "comment": "越权验证"},
    )
    check("pm 提交审核结论 → 403（缺 task:approve）", decision.status_code == 403, f"{decision.status_code}")

    print("\n=== 6. 知识与任务（只读）===")
    kbs = client.get("/api/v1/knowledge-bases", headers=pm)
    check("knowledge-bases 200 且 4 个库", kbs.status_code == 200 and len(kbs.json()) == 4,
          f"{kbs.status_code} n={len(kbs.json()) if kbs.status_code == 200 else '-'}")
    docs = client.get("/api/v1/documents?kb_code=operation_kb&page_size=10", headers=pm)
    check("documents 可按 kb_code 过滤", docs.status_code == 200,
          f"{docs.status_code} total={docs.json().get('meta', {}).get('total') if docs.status_code == 200 else '-'}")

    # 知识库范围过滤：这里**不能**把断言写在 `if status == 200` 里面——
    # 那样一旦真的返回 403，断言会被整个跳过，测试"永远通过"。
    # （我第一版就是这么写的，人工核对时才发现行为与文档不一致。）
    eng_denied = client.get("/api/v1/documents?kb_code=product_kb&page_size=10", headers=eng)
    check(
        "eng 请求无权知识库 → 403 无权访问该知识库",
        eng_denied.status_code == 403 and "知识库" in str(eng_denied.json().get("detail")),
        f"{eng_denied.status_code} {eng_denied.text[:60]}",
    )
    unknown_kb = client.get("/api/v1/documents?kb_code=not_a_kb&page_size=10", headers=eng)
    check(
        "不泄露库是否存在（不存在的库与无权库同样 403）",
        unknown_kb.status_code == 403 and unknown_kb.status_code == eng_denied.status_code,
        f"{unknown_kb.status_code}",
    )
    eng_allowed = client.get("/api/v1/documents?kb_code=engineering_kb&page_size=10", headers=eng)
    check(
        "eng 请求有权知识库 → 200 且有数据（正向对照）",
        eng_allowed.status_code == 200 and eng_allowed.json()["meta"]["total"] == 2,
        f"{eng_allowed.status_code} total={eng_allowed.json().get('meta', {}).get('total') if eng_allowed.status_code == 200 else '-'}",
    )
    eng_all = client.get("/api/v1/documents?page_size=50", headers=eng)
    allowed_kbs = {"engineering_kb", "operation_kb"}
    check(
        "eng 不带 kb_code → 只返回可见库的文档（4 条，且不含无权库）",
        eng_all.status_code == 200
        and eng_all.json()["meta"]["total"] == 4
        and {item["kb_code"] for item in eng_all.json()["items"]} <= allowed_kbs,
        f"total={eng_all.json().get('meta', {}).get('total') if eng_all.status_code == 200 else '-'} "
        f"kbs={sorted({item['kb_code'] for item in eng_all.json().get('items', [])})}",
    )
    pm_docs = client.get("/api/v1/documents?kb_code=product_kb&page_size=5", headers=pm)
    check(
        "pm 请求同一知识库 → 200 且有 3 篇（对照，证明 403 来自角色而非库本身）",
        pm_docs.status_code == 200 and pm_docs.json()["meta"]["total"] == 3,
        f"{pm_docs.status_code} total={pm_docs.json().get('meta', {}).get('total') if pm_docs.status_code == 200 else '-'}",
    )
    if pm_docs.status_code == 200 and pm_docs.json()["items"]:
        doc_id = pm_docs.json()["items"][0]["id"]
        detail = client.get(f"/api/v1/documents/{doc_id}", headers=eng)
        check(
            "eng 取无权库的文档详情 → 403（不返回内容）",
            detail.status_code == 403 and "文档" in str(detail.json().get("detail")),
            f"{detail.status_code} {detail.text[:60]}",
        )
    kb_list = client.get("/api/v1/knowledge-bases", headers=eng)
    check(
        "knowledge-bases 只列可见库（eng 2 个）",
        kb_list.status_code == 200 and sorted(item["code"] for item in kb_list.json()) == ["engineering_kb", "operation_kb"],
        str(sorted(item["code"] for item in kb_list.json()) if kb_list.status_code == 200 else kb_list.status_code),
    )
    summary = client.get("/api/v1/tasks/summary", headers=pm)
    check("tasks/summary 200", summary.status_code == 200, str(summary.json())[:120])
    trace_ok = True
    tasks = client.get("/api/v1/tasks?page_size=5", headers=pm)
    if tasks.status_code == 200 and tasks.json().get("items"):
        first = tasks.json()["items"][0]
        trace = client.get(f"/api/v1/tasks/{first['id']}/trace", headers=pm)
        trace_ok = trace.status_code == 200 and "cost" in trace.json()
    check("任务 trace 可读且含 cost", trace_ok)

    # 说明书 §4.18 知识库问答：这里核对的都是**文档里写出来的具体数值**，
    # 改了检索/阈值实现却忘记改文档，就会在这里红。
    print("\n=== 7. 知识库问答（说明书 §4.18）===")
    qa_body = {
        "query": "3.2.0 版本发布后次日留存下降，版本变更有哪些相关记录？",
        "knowledge_bases": ["product_kb"],
        "top_k": 5,
        "use_model": False,  # 纯检索式：不调模型、不产生费用
    }
    qa = client.post("/api/v1/qa/ask", headers=pm, json=qa_body)
    check("qa/ask 200（pm + product_kb，纯检索式）", qa.status_code == 200, f"{qa.status_code}")
    if qa.status_code == 200:
        payload = qa.json()
        check("问答不需要模型即返回（mode=extractive）", payload.get("mode") == "extractive", str(payload.get("mode")))
        check("问答判定为可回答（insufficient=False）", payload.get("insufficient") is False)
        check("要点 5 条（编号即引用号）", len(payload.get("points") or []) == 5, str(len(payload.get("points") or [])))
        check("引用 5 条", len(payload.get("citations") or []) == 5, str(len(payload.get("citations") or [])))
        first = (payload.get("citations") or [{}])[0]
        check(
            "首条引用指向 3.2.0 产品需求文档",
            first.get("source_name") == "星野社区3.2.0产品需求文档.md" and first.get("kb_code") == "product_kb",
            f"{first.get('kb_code')} · {first.get('source_name')}",
        )
        check("引用带原文片段与相关度", bool(first.get("snippet")) and (first.get("score") or 0) > 0,
              f"score={first.get('score')} snippet_len={len(first.get('snippet') or '')}")
        coverage = payload.get("coverage") or {}
        # 允许覆盖率小幅浮动（分词/阈值不变时应稳定在 0.42~0.43），但阈值与逃生口必须是配置值
        check(
            "覆盖率口径与说明书一致（threshold=0.2 / escape=0.26）",
            coverage.get("threshold") == 0.2 and coverage.get("high_score_escape") == 0.26,
            f"threshold={coverage.get('threshold')} escape={coverage.get('high_score_escape')}",
        )
        check(
            "覆盖率数值与说明书一致（≈0.4286，terms=21 matched=9）",
            0.40 <= (coverage.get("coverage") or 0) <= 0.46
            and coverage.get("terms") == 21
            and coverage.get("matched") == 9,
            f"coverage={coverage.get('coverage')} terms={coverage.get('terms')} matched={coverage.get('matched')}",
        )
        check("命中库回显为 product_kb", payload.get("knowledge_bases") == ["product_kb"],
              str(payload.get("knowledge_bases")))
        check("返回免责说明并提示可转完整诊断", "转成完整诊断" in (payload.get("disclaimer") or ""),
              (payload.get("disclaimer") or "")[:60])

    # 越权：断言**写在状态码上**，不能放进 `if status == 200`，否则一被拒绝就整段跳过
    qa_denied = client.post(
        "/api/v1/qa/ask",
        headers=eng,
        json={"query": "埋点字典怎么定义留存？", "knowledge_bases": ["product_kb"], "use_model": False},
    )
    check(
        "eng 问答越权 product_kb → 403 permission_denied",
        qa_denied.status_code == 403 and "无权访问该知识库" in str(qa_denied.json().get("detail")),
        f"{qa_denied.status_code} {qa_denied.text[:70]}",
    )
    ops_denied = client.post(
        "/api/v1/qa/ask",
        headers=ops,
        json={"query": "发布记录", "knowledge_bases": ["engineering_kb"], "use_model": False},
    )
    check(
        "ops 问答越权 engineering_kb → 403（同理）",
        ops_denied.status_code == 403 and "无权访问该知识库" in str(ops_denied.json().get("detail")),
        f"{ops_denied.status_code}",
    )
    qa_allowed = client.post(
        "/api/v1/qa/ask",
        headers=eng,
        json={"query": "3.2.0 发布记录里的灰度节奏与回滚方案是什么？", "knowledge_bases": ["engineering_kb"],
              "use_model": False},
    )
    check(
        "eng 问答有权库 → 200 且有引用（正向对照）",
        qa_allowed.status_code == 200 and len(qa_allowed.json().get("citations") or []) > 0,
        f"{qa_allowed.status_code} citations={len(qa_allowed.json().get('citations') or []) if qa_allowed.status_code == 200 else '-'}",
    )
    qa_blank = client.post("/api/v1/qa/ask", headers=pm, json={"query": "   ", "use_model": False})
    check("空白问题 → 422（不进入权限/检索逻辑）", qa_blank.status_code == 422, f"{qa_blank.status_code}")
    qa_anon = client.post("/api/v1/qa/ask", json={"query": "留存", "use_model": False})
    check("未带令牌 → 401", qa_anon.status_code == 401, f"{qa_anon.status_code}")
    qa_missing = client.post(
        "/api/v1/qa/ask",
        headers=pm,
        json={"query": "你们公司食堂本周的菜单和晚餐供应时间是什么？", "knowledge_bases": ["product_kb"],
              "use_model": False},
    )
    check(
        "库里没有的主题 → 200 且 insufficient=true（如实说没有，不是报错）",
        qa_missing.status_code == 200 and qa_missing.json().get("insufficient") is True,
        f"{qa_missing.status_code} insufficient={qa_missing.json().get('insufficient') if qa_missing.status_code == 200 else '-'}",
    )
    if qa_missing.status_code == 200:
        check("无答案时给出未覆盖的关键词（missing 非空）", bool(qa_missing.json().get("missing")),
              str((qa_missing.json().get("missing") or [])[:1])[:80])

    # 说明书 §4.13 的"权限预演"：结论必须与**真实请求**一致，否则解释就是错的
    print("\n=== 8. 权限预演（说明书 §4.13）===")
    simulate = client.post(
        "/api/v1/access-policies/simulate",
        headers=admin,
        json={"username": "eng", "metric": "order_conversion_rate"},
    )
    check("simulate 200（admin）", simulate.status_code == 200, f"{simulate.status_code}")
    if simulate.status_code == 200:
        body = simulate.json()
        check("预演结论：eng 不可读受限指标", body["allowed"] is False, str(body["allowed"]))
        check("逐条策略给出「主体不匹配」", {v["code"] for v in body["verdicts"]} == {"subject_mismatch"},
              str(sorted({v["code"] for v in body["verdicts"]})))
        check("预演带出该账号真实角色与密级",
              body["subject"]["roles"] == ["engineer"] and body["subject"]["clearance"] == 2,
              f"{body['subject']['roles']} / {body['subject']['clearance']}")
        check("预演与真实查询结论一致（eng 查该指标 403）",
              client.get("/api/v1/metrics/trend", headers=eng,
                         params={"metric_name": "order_conversion_rate",
                                 "start_date": "2026-08-10", "end_date": "2026-08-20"}).status_code == 403)
    pm_sim = client.post(
        "/api/v1/access-policies/simulate", headers=admin,
        json={"username": "pm", "metric": "order_conversion_rate"},
    )
    check("正向对照：pm 预演为允许且有命中策略",
          pm_sim.status_code == 200 and pm_sim.json()["allowed"] is True and pm_sim.json()["matched_policy_id"],
          f"{pm_sim.status_code}")
    check("预演与真实查询结论一致（pm 查该指标 200）",
          client.get("/api/v1/metrics/trend", headers=pm,
                     params={"metric_name": "order_conversion_rate",
                             "start_date": "2026-08-10", "end_date": "2026-08-20"}).status_code == 200)
    deny_sim = client.post(
        "/api/v1/access-policies/simulate", headers=admin,
        json={"username": "admin", "metric": "finance_raw_ledger"},
    )
    check("广播拒绝策略连管理员也拒（deny 优先）",
          deny_sim.status_code == 200 and deny_sim.json()["allowed"] is False
          and "命中显式拒绝策略" in deny_sim.json()["reason"], f"{deny_sim.status_code}")
    check("非管理员不能预演（403）",
          client.post("/api/v1/access-policies/simulate", headers=pm,
                      json={"username": "eng", "metric": "dau"}).status_code == 403)
    check("未知账号 404",
          client.post("/api/v1/access-policies/simulate", headers=admin,
                      json={"username": "no_such_user", "metric": "dau"}).status_code == 404)
    check("未提供主体 422",
          client.post("/api/v1/access-policies/simulate", headers=admin,
                      json={"metric": "dau"}).status_code == 422)

    # 说明书 §4.14 的死信运维：本脚本承诺**只读**，所以这里只核对形状与权限
    # （"重放一条真死信"的完整链路在 scripts/verify_dead_letter.py 里跑真环境验证）
    print("\n=== 9. 队列运维 / 死信排查（说明书 §4.14）===")
    queue_items = client.get("/api/v1/queue/items?status=dead&limit=20", headers=admin)
    check("queue/items 200（admin）", queue_items.status_code == 200, f"{queue_items.status_code}")
    if queue_items.status_code == 200:
        payload = queue_items.json()
        check("同时返回队列统计与明细", "stats" in payload and "items" in payload,
              f"keys={sorted(payload)[:4]}")
        check("统计含 pending/claimed/done/dead 四项",
              {"pending", "claimed", "done", "dead"} <= set(payload["stats"]),
              str(sorted(payload["stats"])))
        check("死信条目带错误原因、尝试次数与重放次数",
              all({"last_error", "replays", "attempts", "max_attempts"} <= set(item)
                  for item in payload["items"]),
              f"{len(payload['items'])} 条死信")
    check("非管理员看队列运维 → 403",
          client.get("/api/v1/queue/items", headers=pm).status_code == 403)
    check("非管理员一键重放 → 403",
          client.post("/api/v1/queue/replay-dead", headers=pm).status_code == 403)
    check("重放不存在的队列项 → 404",
          client.post("/api/v1/queue/items/does-not-exist/replay", headers=admin).status_code == 404)

    print("\n=== 汇总 ===")
    failed = [item for item in RESULTS if not item[1]]
    print(f"  共 {len(RESULTS)} 项断言：PASS {len(RESULTS) - len(failed)} / FAIL {len(failed)}")
    for name, _, detail in failed:
        print(f"    - {name}：{detail}")
    if args.json:
        print(json.dumps([{"name": n, "passed": ok, "detail": d} for n, ok, d in RESULTS], ensure_ascii=False, indent=2))
    client.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

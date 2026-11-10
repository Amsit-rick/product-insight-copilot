"""端到端演示脚本：跑通「创建任务 → 工作流 → 人工审核 → 报告」的完整故事。

用法（在 backend 目录下）：

    $env:PYTHONPATH = $PWD
    ..\\.venv\\Scripts\\python.exe scripts\\demo_e2e.py

前置条件：已执行 `python -m app.db.seed`（有演示账号与演示数据）。
脚本走的是真实 HTTP 接口（TestClient 进程内），因此和部署后手动点击的效果一致。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

QUESTION = (
    "Android 端 3.2.0 发布后次日留存下降，哪些用户受到影响？"
    "版本变更、用户反馈和故障记录是否支持版本相关性？下一步应该优先验证什么？"
)
FILTERS = {
    "product": "星野社区",
    "platform": "Android",
    "app_version": "3.2.0",
    "date_from": "2026-08-01",
    "date_to": "2026-08-31",
    "knowledge_bases": ["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
    "metrics": ["next_day_retention", "crash_rate"],
    "priority": "P1",
}


def _token(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    resp.raise_for_status()
    return resp.json()["access_token"]


def _wait(client: TestClient, headers: dict, task_id: str, *, stop_states: set[str]) -> dict:
    deadline = time.time() + 180
    detail = client.get(f"/api/v1/tasks/{task_id}", headers=headers).json()
    while time.time() < deadline and detail["status"] not in stop_states:
        time.sleep(0.5)
        detail = client.get(f"/api/v1/tasks/{task_id}", headers=headers).json()
    return detail


def main() -> int:
    with TestClient(app) as client:
        health = client.get("/health").json()
        print(f"服务: {health['app']} | 数据库={health['database']} | 模型={health['llm']['model']} "
              f"| 向量={health['vector_backend']}")

        pm = {"Authorization": f"Bearer {_token(client, 'pm', 'pm123')}"}
        reviewer = {"Authorization": f"Bearer {_token(client, 'reviewer', 'reviewer123')}"}

        print("\n[1/5] 创建诊断任务")
        created = client.post("/api/v1/tasks", headers=pm, json={"query": QUESTION, **FILTERS})
        created.raise_for_status()
        task_id = created.json()["id"]
        print(f"      task_id = {task_id}")

        print("\n[2/5] 等待工作流执行（并行 Agent → 证据合并 → 分支判定）")
        detail = _wait(client, pm, task_id, stop_states={"waiting_human", "completed", "failed"})
        print(f"      状态={detail['status']} 意图={detail['intent']} 风险={detail['risk_level']}")
        trace = client.get(f"/api/v1/tasks/{task_id}/trace", headers=pm).json()
        for run in trace["agent_runs"]:
            print(
                f"      - {run['agent_code']:<14} {run['status']:<10} "
                f"结论 {run['findings']:>2} 条  耗时 {run['latency_ms']}ms  模型={run['model']}"
            )
        print(
            f"      工具调用 {trace['totals']['tool_calls']} 次，"
            f"被策略拒绝 {trace['totals']['denied_tool_calls']} 次"
        )

        if detail["status"] == "waiting_human":
            print(f"\n[3/5] 人工审核：{detail['waiting_reason']}")
            payload = next(
                item
                for item in client.get("/api/v1/approvals", headers=reviewer).json()["items"]
                if item["task_id"] == task_id
            )
            request = payload["request"]
            print(
                f"      证据 {request['coverage']['evidence_count']} 条 / "
                f"冲突 {len(request['conflicts'])} 处 / 指标可用={request['coverage']['metric_available']}"
            )
            for conflict in request["conflicts"]:
                print(f"      冲突主题：{conflict['topic']}")
                print(f"        A（{conflict['statement_a']['agent_code']}）："
                      f"{conflict['statement_a']['text'][:70]}")
                print(f"        B（{conflict['statement_b']['agent_code']}）："
                      f"{conflict['statement_b']['text'][:70]}")
            client.post(
                f"/api/v1/tasks/{task_id}/approve",
                headers=reviewer,
                json={"action": "approve", "comment": "演示：已核对冲突双方证据，同意出报告"},
            ).raise_for_status()
            detail = _wait(client, pm, task_id, stop_states={"completed", "failed"})
            print(f"      审核后状态 = {detail['status']}")
        else:
            print("\n[3/5] 无需人工审核，直接出报告")

        print("\n[4/5] 报告要点")
        report_resp = client.get(f"/api/v1/tasks/{task_id}/report", headers=pm)
        if report_resp.status_code != 200:
            print(f"      没有报告：{report_resp.text}")
            return 1
        report = report_resp.json()
        print(f"      优先级={report['priority']} 置信度={report['confidence']} "
              f"审核={report['approval_status']} 需人工={report['needs_human_review']}")
        print(f"      事实 {len(report['facts'])} / 推断 {len(report['inferences'])} / "
              f"假设 {len(report['hypotheses'])} / 建议 {len(report['recommendations'])} / "
              f"冲突 {len(report['conflicts'])} / 引用 {len(report['citations'])}")
        print(f"\n      摘要：{report['summary'][:300]}")
        print("\n      影响范围：")
        for item in report["impact_scope"][:5]:
            print(f"        - {item['dimension']} = {item['value']}  {item.get('evidence_refs')}")
        print("\n      关键事实（含引用编号）：")
        for fact in report["facts"][:5]:
            marks = " ".join(report["citation_marks"].get(r, "").split(" ")[0]
                             for r in fact.get("evidence_refs") or [])
            print(f"        - {fact['text'][:90]} {marks}")
        if report["conflicts"]:
            print("\n      证据冲突（未合并）：")
            for conflict in report["conflicts"]:
                resolution = conflict.get("resolution") or {}
                print(f"        - {conflict['topic']}：采用 {resolution.get('adopted_statement')}"
                      f"（{resolution.get('adopted_source')}），{resolution.get('reason')}")
        if report["data_gaps"]:
            print(f"\n      数据缺口：{report['data_gaps']}")

        print("\n[5/5] 导出与审计")
        export = client.get(f"/api/v1/reports/{report['id']}/export?format=md", headers=pm)
        out_path = Path(BACKEND_DIR) / "data" / f"demo_report_{report['id'][:8]}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(export.text, encoding="utf-8")
        print(f"      Markdown 报告已写入 {out_path}")

        admin = {"Authorization": f"Bearer {_token(client, 'admin', 'admin123')}"}
        audit = client.get("/api/v1/audit-logs?limit=8", headers=admin).json()
        print(f"      最近审计动作：{[item['action'] for item in audit['items']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

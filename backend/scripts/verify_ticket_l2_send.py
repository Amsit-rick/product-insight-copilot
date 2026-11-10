"""对着运行中的服务验一遍本轮新增/修改的两条路径（stub，零成本）。

1. `GET /tickets/assignees`：客服自己能拿到转派候选人，研发不在名单里；
2. L2（staff_confirm）写操作建议：客服「确认并发送」能走通 → 工单已解决，且审计里有确认记录；
3. L3（supervisor）建议：仍然发不出去（409），门禁没放松。
"""

import json
import os
import sys
import time

import httpx

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8010")
client = httpx.Client(base_url=BASE, timeout=30)

ok = 0
fail = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def wait_recommendation(ticket_id: str, *, timeout: float = 120.0):
    """等分析真正跑完再取建议。

    为什么必须等：服务默认是 threadpool 后端，`analyze` 只是派发就返回；
    不等就会读到 None，然后把"还没跑完"误报成缺陷（评测接口踩过同一个坑）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = client.get(f"/api/v1/tickets/{ticket_id}/recommendation", headers=H).json()
        if rec is not None:
            return rec
        time.sleep(1.0)
    return None


token = client.post("/api/v1/auth/login", json={"username": "ops", "password": "ops123"}).json()[
    "access_token"
]
H = {"Authorization": f"Bearer {token}"}
R = {**H, "X-Requested-With": "verify"}

print("=== 1/3 转派候选人 ===")
resp = client.get("/api/v1/tickets/assignees", headers=H)
check("接口可用", resp.status_code == 200, f"status={resp.status_code}")
names = {item["username"] for item in resp.json()["items"]}
check("客服自己在候选人里", "ops" in names, str(sorted(names)))
check("主管在候选人里（L3 要转给他）", "reviewer" in names)
check("研发不在候选人里", "eng" not in names)

eng_token = client.post(
    "/api/v1/auth/login", json={"username": "eng", "password": "eng123"}
).json()["access_token"]
check(
    "无 ticket:assign 的角色看不到候选人列表",
    client.get(
        "/api/v1/tickets/assignees", headers={"Authorization": f"Bearer {eng_token}"}
    ).status_code
    == 403,
)

print("\n=== 2/3 L2 写操作：客服确认并发送 ===")
created = client.post(
    "/api/v1/tickets",
    json={
        "subject": "验证：破损补发（L2）",
        "content": "收到商品外包装破损，担心影响使用。",
        "external_id": "DEBUG-L2-SEND",
        "order_id": "TB202609240992",
        "data_origin": "test",
    },
    headers=H,
)
check("建单成功", created.status_code == 201, f"status={created.status_code}")
ticket = created.json()
analyzed = client.post(f"/api/v1/tickets/{ticket['id']}/analyze", headers=H)
check("分析已受理", analyzed.status_code == 202, f"status={analyzed.status_code}")

rec = wait_recommendation(ticket["id"])
if rec is None:
    check("分析产出建议", False, "超时未产出建议（执行后端可能在别的进程）")
else:
    print(
        f"    建议：action={rec['action_type']} tier={rec['risk_tier']} "
        f"approval={rec['approval_requirement']} status={rec['status']} "
        f"blocked={rec['write_action_blocked']}"
    )
    if rec["approval_requirement"] == "staff_confirm":
        check("L2 写操作不被标记为'禁止执行'", rec["write_action_blocked"] is False)
        sent = client.post(f"/api/v1/recommendations/{rec['id']}/send", headers=H)
        check("客服确认并发送成功", sent.status_code == 200, f"status={sent.status_code}")
        detail = client.get(f"/api/v1/tickets/{ticket['id']}", headers=H).json()
        check("工单已解决", detail["ticket"]["status"] == "resolved", detail["ticket"]["status"])
        check(
            "回复进了消息流",
            any(m["sender_type"] == "staff" for m in detail["messages"]),
        )
        # 审计要直接查库（详情接口把审计渲染成时间线，没有单独的 audits 字段）
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        from app.db.session import SessionLocal
        from app.models.workflow import AuditLog

        session = SessionLocal()
        try:
            actions = [
                row.action
                for row in session.query(AuditLog)
                .filter(AuditLog.resource_id == rec["id"])
                .all()
            ]
        finally:
            session.close()
        check(
            "审计留了客服确认记录",
            "recommendation_confirmed_by_staff" in actions,
            str(actions),
        )
    else:
        check("该工单命中 L2（staff_confirm）", False, rec["approval_requirement"])

print("\n=== 3/3 L3 仍然不能自助发送 ===")
l3 = client.post(
    "/api/v1/tickets",
    json={
        "subject": "验证：退款（L3）",
        "content": "商品不想要了，申请退款，请把钱退给我。",
        "external_id": "DEBUG-L3-SEND",
        "order_id": "TB202609241501",
        "data_origin": "test",
    },
    headers=H,
).json()
client.post(f"/api/v1/tickets/{l3['id']}/analyze", headers=H)
l3rec = wait_recommendation(l3["id"])
if l3rec is None:
    check("L3 分析产出建议", False, "超时未产出建议")
    l3rec = {"action_type": "?", "risk_tier": "?", "approval_requirement": "?"}
print(
    f"    建议：action={l3rec['action_type']} tier={l3rec['risk_tier']} "
    f"approval={l3rec['approval_requirement']} status={l3rec['status']}"
)
check("L3 建议不进待发送状态", l3rec["status"] == "pending_approval", l3rec["status"])
blocked = client.post(f"/api/v1/recommendations/{l3rec['id']}/send", headers=H)
check("L3 未批准发送被拒（409）", blocked.status_code == 409, f"status={blocked.status_code}")
check(
    "拒绝原因是 recommendation_not_approved",
    blocked.json()["detail"]["code"] == "recommendation_not_approved",
)

print(f"\n=== 汇总：{ok}/{ok + fail} 通过 ===")
sys.exit(0 if fail == 0 else 1)

"""集成测试：队列运维接口（死信排查与重放）。

三条底线：
1. **只有管理员**能用 —— "哪些任务在队列里死了、错误原因是什么"本身就是敏感信息；
2. 只允许重放 **dead** 项 —— 把 pending/done 重新放回会造成同一任务两条队列项；
3. 重放要留痕（审计），否则"谁把死信放回去了"查不出来。
"""

from __future__ import annotations

from app.db.session import SessionLocal
from app.models.base import utcnow
from app.models.queue import TaskQueueItem

QUEUE = "/api/v1/queue"


def _add_item(*, task_id: str, status: str = "dead", attempts: int = 3, last_error: str = "boom") -> str:
    """直接往队列表塞一条记录（队列项没法从接口"造出死信"，只能这样准备数据）。"""
    session = SessionLocal()
    try:
        row = TaskQueueItem(
            task_id=task_id,
            kind="run",
            payload={},
            status=status,
            attempts=attempts,
            max_attempts=3,
            last_error=last_error,
            finished_at=utcnow() if status == "dead" else None,
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def _status_of(item_id: str) -> tuple[str, int]:
    session = SessionLocal()
    try:
        session.expire_all()
        row = session.get(TaskQueueItem, item_id)
        return (row.status, int(row.attempts or 0))
    finally:
        session.close()


def test_仅管理员可访问队列运维(client, auth):
    assert client.get(f"{QUEUE}/items", headers=auth["pm"]).status_code == 403
    assert client.get(f"{QUEUE}/items", headers=auth["eng"]).status_code == 403
    assert client.post(f"{QUEUE}/replay-dead", headers=auth["pm"]).status_code == 403


def test_队列项列表返回统计与明细(client, auth):
    item_id = _add_item(task_id="task-visibility", last_error="ValueError: 任务不存在")
    resp = client.get(f"{QUEUE}/items?status=dead", headers=auth["admin"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "stats" in body and "items" in body
    assert set(body["stats"]) >= {"pending", "claimed", "done", "dead"}

    found = [item for item in body["items"] if item["id"] == item_id]
    assert found, "刚造的死信应当出现在列表里"
    assert found[0]["status"] == "dead"
    assert "任务不存在" in found[0]["last_error"]
    assert found[0]["replays"] == 0
    # 任务不存在时也要能看出来（否则光看 task_id 没法判断）
    assert found[0]["task_missing"] is True


def test_状态筛选走白名单(client, auth):
    resp = client.get(f"{QUEUE}/items?status=whatever", headers=auth["admin"])
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_status"


def test_重放不存在的队列项返回_404(client, auth):
    resp = client.post(f"{QUEUE}/items/does-not-exist/replay", headers=auth["admin"])
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


def test_只允许重放死信(client, auth):
    pending_id = _add_item(task_id="task-pending", status="pending", attempts=0, last_error=None)
    resp = client.post(f"{QUEUE}/items/{pending_id}/replay", headers=auth["admin"])
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "not_dead"


def test_重放死信后回到待领取并清零尝试次数(client, auth):
    item_id = _add_item(task_id="task-replay", attempts=3, last_error="RuntimeError: 炸了")
    # 集成测试共用一个临时库，别的用例也可能留下死信 —— 所以比的是**增量**而不是绝对值
    before_dead = client.get(f"{QUEUE}/items", headers=auth["admin"]).json()["stats"]["dead"]

    resp = client.post(f"{QUEUE}/items/{item_id}/replay", headers=auth["admin"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replayed"] is True
    assert body["stats"]["dead"] == before_dead - 1, "这条死信应当从 dead 里消失"

    status_value, attempts = _status_of(item_id)
    assert status_value == "pending"
    assert attempts == 0, "attempts 必须清零，否则下次失败立刻又判死"


def test_重放写审计(client, auth):
    item_id = _add_item(task_id="task-audit")
    before = client.get("/api/v1/audit-logs?action=queue_item_replayed&limit=100", headers=auth["admin"]).json()
    client.post(f"{QUEUE}/items/{item_id}/replay", headers=auth["admin"])
    after = client.get("/api/v1/audit-logs?action=queue_item_replayed&limit=100", headers=auth["admin"]).json()
    assert len(after["items"]) == len(before["items"]) + 1
    assert after["items"][0]["resource_id"] == item_id


def test_一键重放全部死信(client, auth):
    first = _add_item(task_id="task-batch-1")
    second = _add_item(task_id="task-batch-2")
    live = _add_item(task_id="task-batch-3", status="done", attempts=1, last_error=None)

    resp = client.post(f"{QUEUE}/replay-dead", headers=auth["admin"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["replayed"] >= 2
    assert resp.json()["stats"]["dead"] == 0

    assert _status_of(first)[0] == "pending"
    assert _status_of(second)[0] == "pending"
    assert _status_of(live)[0] == "done", "非死信不受影响"

"""任务级回放 API：播放历史快照，不重新调用模型或工具。"""

from __future__ import annotations


def _create_task(client, auth) -> dict:
    response = client.post(
        "/api/v1/tasks",
        headers=auth["pm"],
        json={
            "query": "分析 Android 3.2.0 版本留存变化",
            "knowledge_bases": ["product_kb", "operation_kb"],
            "idempotency_key": "replay-api-task-v1",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_任务回放快照区分播放与重新执行(client, auth):
    task = _create_task(client, auth)

    denied = client.post(
        f"/api/v1/tasks/{task['id']}/replay-snapshots",
        headers=auth["pm"],
    )
    assert denied.status_code == 403

    created = client.post(
        f"/api/v1/tasks/{task['id']}/replay-snapshots",
        headers=auth["reviewer"],
    )
    assert created.status_code == 201, created.text
    snapshot = created.json()
    assert snapshot["mode"] == "playback"
    assert snapshot["reran_model_or_tools"] is False
    assert len(snapshot["digest"]) == 64
    assert snapshot["package"]["playback_fidelity"] == "complete"
    assert snapshot["package"]["tool_calls"]
    assert all(call["result"] is not None for call in snapshot["package"]["tool_calls"])
    assert all(call["result_digest"] for call in snapshot["package"]["tool_calls"])

    replay_denied = client.get(f"/api/v1/tasks/{task['id']}/replay", headers=auth["pm"])
    assert replay_denied.status_code == 403

    replay = client.get(f"/api/v1/tasks/{task['id']}/replay", headers=auth["reviewer"])
    assert replay.status_code == 200, replay.text
    assert replay.json()["snapshot_id"] == snapshot["snapshot_id"]
    assert replay.json()["package"]["task"]["id"] == task["id"]

    verified = client.post(
        f"/api/v1/tasks/{task['id']}/replay/verify",
        headers=auth["reviewer"],
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["valid"] is True


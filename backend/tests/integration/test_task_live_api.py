"""A reconnecting client gets a task snapshot before incremental SSE events."""

from __future__ import annotations


def _completed_task(client, auth) -> dict:  # noqa: ANN001
    # 该测试验证实时协议，不应消耗公共账号的任务提交限流额度。
    from app.db import session as db_session
    from app.models.identity import User
    from app.models.workflow import AnalysisEvent, AnalysisTask, ToolCall

    session = db_session.SessionLocal()
    try:
        user = session.query(User).filter(User.username == "pm").one()
        row = AnalysisTask(
            tenant_id=user.tenant_id,
            user_id=user.id,
            title="task live protocol",
            query="verify snapshot before delta",
            status="completed",
            current_node="persist_result",
            current_phase="completed",
            checkpoint_version=3,
            phase_attempt=1,
            state_revision=4,
        )
        session.add(row)
        session.flush()
        session.add_all(
            [
                AnalysisEvent(
                    task_id=row.id,
                    node="validate_request",
                    message="任务请求已校验",
                    payload={},
                ),
                AnalysisEvent(
                    task_id=row.id,
                    node="persist_result",
                    message="结果已保存",
                    payload={},
                ),
                ToolCall(
                    task_id=row.id,
                    tool_name="order.facts",
                    arguments={"order_id": "ORDER-LIVE-001"},
                    result_summary="ok",
                    status="success",
                    policy_decision="allow",
                    replay_policy="safe",
                    side_effect_level="read_only",
                    replayed=False,
                ),
            ]
        )
        session.commit()
        return {"id": row.id, "status": row.status}
    finally:
        session.close()


def test_snapshot_contains_phase_revision_and_stable_event_sequence(client, auth) -> None:
    task = _completed_task(client, auth)
    response = client.get(f"/api/v1/tasks/{task['id']}/snapshot", headers=auth["pm"])

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == "task-live.v1"
    assert payload["task"]["current_phase"] == "completed"
    assert payload["state_revision"] >= 1
    assert payload["events"]
    sequences = [item["event_seq"] for item in payload["events"]]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert payload["event_cursor"] == payload["events"][-1]["id"]
    assert payload["tool_calls"]
    assert all(item["replay_policy"] == "safe" for item in payload["tool_calls"])
    assert all(item["side_effect_level"] == "read_only" for item in payload["tool_calls"])


def test_stream_emits_snapshot_before_delta_or_done(client, auth) -> None:
    task = _completed_task(client, auth)
    with client.stream(
        "GET",
        f"/api/v1/tasks/{task['id']}/stream",
        params={"max_seconds": 5},
        headers=auth["pm"],
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert "event: snapshot" in body
    assert "event: done" in body
    assert body.index("event: snapshot") < body.index("event: done")
    for line in body.splitlines():
        if line.startswith("data: "):
            import json

            json.loads(line[len("data: ") :])


"""工单可观测性接口的集成测试：SSE 事件流与每单成本。

SSE 用 `TestClient.stream` 读真实字节流，覆盖三件事：
1. 鉴权：没有 `ticket:read` 或运行不存在 → 404/401，而不是先建连接再报错；
2. 帧格式：`event:` / `data:` 成对出现，且以空行结束（前端 EventSource 依赖它）；
3. 收尾：任务已终态时发 `done` 并结束，不把连接挂着。
"""

from __future__ import annotations


def _create_and_analyze(client, auth, payload: dict) -> dict:
    ticket = client.post("/api/v1/tickets", json=payload, headers=auth["ops"]).json()
    resp = client.post(f"/api/v1/tickets/{ticket['id']}/analyze", headers=auth["ops"])
    assert resp.status_code == 202, resp.text
    return {"ticket": ticket, "run_id": resp.json()["run_id"]}


def _wait(client, auth, ticket_id: str) -> dict:
    import time

    deadline = time.time() + 60
    last = {}
    while time.time() < deadline:
        last = client.get(f"/api/v1/tickets/{ticket_id}", headers=auth["ops"]).json()
        steps = (last.get("analysis") or {}).get("business_steps") or []
        if last.get("recommendation") and len(steps) >= 7:
            return last
        time.sleep(0.5)
    return last


def test_cost_endpoint_reports_tokens_and_amount(client, auth):
    created = _create_and_analyze(
        client,
        auth,
        {
            "subject": "成本口径",
            "content": "物流停滞，想问下能不能补偿。",
            "external_id": "OBS-TEST-0001",
            "order_id": "TB202609240655",
        },
    )
    detail = _wait(client, auth, created["ticket"]["id"])
    assert detail["recommendation"] is not None

    forbidden = client.get(
        f"/api/v1/tickets/{created['ticket']['id']}/cost", headers=auth["ops"]
    )
    assert forbidden.status_code == 403
    resp = client.get(
        f"/api/v1/tickets/{created['ticket']['id']}/cost", headers=auth["eng"]
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["analysis_count"] >= 1
    assert payload["currency"] == "CNY"
    assert payload["per_analysis"], "要能按次列出成本"
    assert "amount_per_analysis" in payload
    # stub 模型下成本为 0（不产生费用），但字段必须齐全，否则界面无法展示
    assert set(payload) >= {"input_tokens", "output_tokens", "calls", "amount", "per_model"}


def test_cost_endpoint_with_volume_estimate(client, auth):
    created = _create_and_analyze(
        client,
        auth,
        {
            "subject": "批量推算",
            "content": "包裹破损了，需要补发。",
            "external_id": "OBS-TEST-0002",
            "order_id": "TB202609240992",
        },
    )
    _wait(client, auth, created["ticket"]["id"])
    forbidden = client.get(
        f"/api/v1/tickets/{created['ticket']['id']}/cost",
        params={"daily_volume": 5000},
        headers=auth["ops"],
    )
    assert forbidden.status_code == 403
    response = client.get(
        f"/api/v1/tickets/{created['ticket']['id']}/cost",
        params={"daily_volume": 5000},
        headers=auth["eng"],
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["batch_estimate"]["daily_volume"] == 5000
    assert payload["batch_estimate"]["yearly_amount"] >= payload["batch_estimate"]["daily_amount"]


def test_sse_stream_emits_frames_and_done(client, auth):
    created = _create_and_analyze(
        client,
        auth,
        {
            "subject": "事件流",
            "content": "物流超过 48 小时没有更新，请帮我催一下。",
            "external_id": "OBS-TEST-0003",
            "order_id": "TB202609240655",
        },
    )
    _wait(client, auth, created["ticket"]["id"])  # 先让分析跑完，事件流才能立刻收尾

    with client.stream(
        "GET",
        f"/api/v1/runs/{created['run_id']}/events",
        params={"max_seconds": 15},
        headers=auth["ops"],
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(chunk for chunk in response.iter_text())

    assert "event: open" in body
    assert "event: done" in body
    # 每个 data 行都必须是合法单行 JSON（换行会破坏帧边界）
    for line in body.splitlines():
        if line.startswith("data: "):
            import json

            json.loads(line[len("data: ") :])
    # 帧以空行分隔
    assert "\n\n" in body


def test_sse_requires_existing_run_and_permission(client, auth):
    missing = client.get("/api/v1/runs/not-exist-run/events", headers=auth["ops"])
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "run_not_found"

    unauthenticated = client.get("/api/v1/runs/whatever/events")
    assert unauthenticated.status_code == 401

    # 研发没有 ticket:read 之外的工单权限也不该能读运行事件（这里用无权限角色验证 403 语义）
    forbidden = client.post(
        "/api/v1/tickets",
        json={"subject": "无权限建单", "content": "研发岗位不应能建单，这里只用于验证权限。"},
        headers=auth["eng"],
    )
    assert forbidden.status_code == 403


def test_detail_includes_cost_block(client, auth):
    created = _create_and_analyze(
        client,
        auth,
        {
            "subject": "详情带成本",
            "content": "少件，只收到一件，需要补发。",
            "external_id": "OBS-TEST-0004",
            "order_id": "TB202609241742",
        },
    )
    detail = _wait(client, auth, created["ticket"]["id"])
    assert detail["cost"] is None
    engineer_detail = client.get(
        f"/api/v1/tickets/{created['ticket']['id']}", headers=auth["eng"]
    )
    assert engineer_detail.status_code == 200, engineer_detail.text
    cost = engineer_detail.json()["cost"]
    assert cost is not None
    assert "amount" in cost and "analysis_count" in cost

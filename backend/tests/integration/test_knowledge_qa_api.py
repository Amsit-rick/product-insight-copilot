"""集成测试：知识库问答（检索即答）。

这个入口最容易出两类问题，必须钉住：

1. **它变成"绕过权限的捷径"**——问答只做检索，所以权限、密级、越权拒绝必须和诊断任务完全一致；
2. **没有资料却硬答**——检索不到时必须如实说"检索不到"，并给出搜索范围，不能编内容。
"""

from __future__ import annotations

from app.db.session import SessionLocal
from app.models.identity import User
from app.models.workflow import AuditLog


def ask(client, auth, query: str, **extra):
    payload = {"query": query, **extra}
    return client.post("/api/v1/qa/ask", headers=auth["pm"], json=payload)


def test_有资料时给出答案要点与可定位引用(client, auth):
    resp = ask(client, auth, "埋点字典里 user_login 事件的口径是怎么定义的？", knowledge_bases=["product_kb"])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["insufficient"] is False
    assert body["citations"], "必须带引用"
    assert body["points"], "必须有答案要点"
    # 引用要能定位：文档名 + 片段 + 知识库
    first = body["citations"][0]
    assert first["source_name"] and first["snippet"] and first["kb_code"] == "product_kb"
    assert first["index"] == 1
    assert body["mode"] in {"extractive", "model"}
    assert "转成完整诊断" in body["disclaimer"], "必须提示这不是严谨结论"


def test_答案要点带引用编号且片段来自原文(client, auth):
    body = ask(client, auth, "3.2.0 的发布记录里写的是升级到哪个登录 SDK 版本？", knowledge_bases=["engineering_kb"]).json()

    assert body["points"]
    assert all(point.startswith("[") for point in body["points"]), "每个要点都应带引用编号"


def test_检索不到时如实说明而不是编造(client, auth):
    body = ask(client, auth, "请给出 2027 年火星基地的会员续费率目标", knowledge_bases=["product_kb"]).json()

    assert body["insufficient"] is True
    assert body["citations"] == [] or body["points"] == []
    assert "没有检索到" in body["answer"]
    assert body["missing"], "必须说明搜索范围与缺失"
    assert "仅供核对" in body["disclaimer"]


def test_显式请求无权知识库被拒绝(client, auth):
    resp = client.post(
        "/api/v1/qa/ask",
        headers=auth["eng"],
        json={"query": "PRD 里新登录流程的设计目标是什么？", "knowledge_bases": ["product_kb"]},
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "permission_denied"
    assert "知识库" in resp.json()["detail"]["message"]


def test_不同角色看到的来源不同(client, auth):
    """eng 只能检索研发/运营库：同一个问题不应返回产品库的引用。"""
    resp = client.post(
        "/api/v1/qa/ask",
        headers=auth["eng"],
        json={"query": "3.2.0 版本有哪些接口变更？", "knowledge_bases": []},
    )

    assert resp.status_code == 200, resp.text
    kbs = {item["kb_code"] for item in resp.json()["citations"]}
    assert kbs <= {"engineering_kb", "operation_kb"}, f"越权来源: {kbs}"


def test_受限指标域问题被拒绝(client, auth):
    """与诊断任务入口一致：财务/经营指标域超出密级时直接拒绝，而不是"检索不到"。"""
    resp = client.post(
        "/api/v1/qa/ask",
        headers=auth["eng"],
        json={"query": "把含营收的经营大盘报表导给我"},
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "permission_denied"
    assert "指标" in resp.json()["detail"]["message"]


def test_空问题被拒绝(client, auth):
    resp = client.post("/api/v1/qa/ask", headers=auth["pm"], json={"query": " "})

    assert resp.status_code == 422


def test_未登录被拒绝(client):
    assert client.post("/api/v1/qa/ask", json={"query": "随便问问"}).status_code == 401


def test_关闭模型归纳也能回答(client, auth):
    """stub/离线环境下必须可用：纯检索式回答，不依赖模型。"""
    body = ask(
        client, auth, "客服工单里用户抱怨最多的是哪几类问题？", knowledge_bases=["feedback_kb"], use_model=False
    ).json()

    assert body["mode"] == "extractive"
    assert body["citations"]


def test_纯检索答案要点不能漏掉同一节后半部分的并列约定(client, auth):
    body = ask(
        client,
        auth,
        "数据质量问题与处理约定是怎么做的？",
        knowledge_bases=["operation_kb"],
        use_model=False,
        top_k=20,
    ).json()

    assert "2026-08-26" in body["answer"]
    assert "iOS 不产出 low_end" in body["answer"]
    assert "上游明细任务调度失败" not in body["answer"]
    assert "(metric_name, platform, app_version" not in body["answer"]
    points = " ".join(body["points"])
    assert "2026-08-26" in points
    assert "iOS" in points and "low_end" in points
    assert len(body["points"]) <= 5
    assert "iOS 不产出 low_end" in body["citations"][0]["snippet"]


def test_问答会写审计(client, auth):
    ask(client, auth, "埋点字典里 user_login 口径是什么？")

    logs = client.get("/api/v1/audit-logs?limit=50", headers=auth["admin"]).json()["items"]
    assert any(item["action"] == "knowledge_qa" for item in logs)


def test_问答记录可回看且按用户隔离(client, auth):
    query = "qa-history-persistence-regression-case"
    response = ask(client, auth, query, knowledge_bases=["product_kb"])
    assert response.status_code == 200, response.text

    history = client.get("/api/v1/qa/history?page=1&page_size=10", headers=auth["pm"])
    assert history.status_code == 200, history.text
    matching = [item for item in history.json()["items"] if item["query"] == query]
    assert len(matching) == 1
    assert matching[0]["answer"]["citations"] == response.json()["citations"]
    assert matching[0]["created_at"]

    other_user_history = client.get("/api/v1/qa/history?page=1&page_size=50", headers=auth["eng"])
    assert other_user_history.status_code == 200, other_user_history.text
    assert query not in {item["query"] for item in other_user_history.json()["items"]}


def test_问答按会话归档且打开后可以继续追问(client, auth):
    first = ask(
        client,
        auth,
        "user_login 的用途是什么？",
        knowledge_bases=["product_kb"],
        use_model=False,
    )
    assert first.status_code == 200, first.text
    conversation_id = first.json().get("conversation_id")
    assert conversation_id

    second = ask(
        client,
        auth,
        "它有哪些关键字段？",
        conversation_id=conversation_id,
        knowledge_bases=["product_kb"],
        use_model=False,
    )
    assert second.status_code == 200, second.text
    assert second.json()["conversation_id"] == conversation_id
    assert second.json()["insufficient"] is False
    second_points = " ".join(second.json()["points"])
    assert "result" in second_points and "fail_code" in second_points

    conversations = client.get("/api/v1/qa/conversations?page=1&page_size=20", headers=auth["pm"])
    assert conversations.status_code == 200, conversations.text
    matching = [item for item in conversations.json()["items"] if item["id"] == conversation_id]
    assert len(matching) == 1
    assert matching[0]["message_count"] == 2
    assert matching[0]["title"].startswith("user_login")

    messages = client.get(
        f"/api/v1/qa/conversations/{conversation_id}/messages?page=1&page_size=20",
        headers=auth["pm"],
    )
    assert messages.status_code == 200, messages.text
    assert [item["query"] for item in messages.json()["items"]] == [
        "user_login 的用途是什么？",
        "它有哪些关键字段？",
    ]


def test_会话不能被其他用户读取或续问(client, auth):
    created = ask(
        client,
        auth,
        "仅当前用户可见的会话",
        knowledge_bases=["product_kb"],
        use_model=False,
    ).json()
    conversation_id = created["conversation_id"]

    assert (
        client.get(
            f"/api/v1/qa/conversations/{conversation_id}/messages",
            headers=auth["eng"],
        ).status_code
        == 404
    )
    append = client.post(
        "/api/v1/qa/ask",
        headers=auth["eng"],
        json={"query": "尝试续问", "conversation_id": conversation_id, "use_model": False},
    )
    assert append.status_code == 404


def test_旧的平铺问答历史会自动归档成可继续的会话(client, auth):
    session = SessionLocal()
    try:
        pm = session.query(User).filter(User.username == "pm").one()
        session.add(
            AuditLog(
                tenant_id=pm.tenant_id,
                actor_id=pm.id,
                action="knowledge_qa",
                resource_type="knowledge_base",
                detail={
                    "query": "旧历史问题：如何查看报表？",
                    "answer": {
                        "query": "旧历史问题：如何查看报表？",
                        "answer": "旧历史答案",
                        "points": [],
                        "citations": [],
                        "insufficient": False,
                        "missing": [],
                        "mode": "extractive",
                        "knowledge_bases": ["operation_kb"],
                        "latency_ms": 1,
                        "model": None,
                        "warnings": [],
                        "coverage": {},
                        "disclaimer": "历史记录",
                    },
                },
            )
        )
        session.commit()
    finally:
        session.close()

    conversations = client.get("/api/v1/qa/conversations?page=1&page_size=100", headers=auth["pm"])
    assert conversations.status_code == 200, conversations.text
    legacy = next(item for item in conversations.json()["items"] if item["title"].startswith("旧历史问题"))
    messages = client.get(
        f"/api/v1/qa/conversations/{legacy['id']}/messages",
        headers=auth["pm"],
    ).json()["items"]
    assert len(messages) == 1
    assert messages[0]["answer"]["answer"] == "旧历史答案"

    continued = ask(
        client,
        auth,
        "继续这个旧会话",
        conversation_id=legacy["id"],
        knowledge_bases=["operation_kb"],
        use_model=False,
    )
    assert continued.status_code == 200, continued.text
    assert continued.json()["conversation_id"] == legacy["id"]


def test_问答不产生任务与报告(client, auth):
    """问答是只读的：不能顺手创建任务或报告（否则"轻量入口"会污染任务列表）。"""
    session = SessionLocal()
    try:
        before_tasks = session.query(__import__("app.models.workflow", fromlist=["AnalysisTask"]).AnalysisTask).count()
        before_reports = session.query(__import__("app.models.workflow", fromlist=["Report"]).Report).count()
    finally:
        session.close()

    ask(client, auth, "3.2.0 发布记录里的登录 SDK 版本是多少？")

    session = SessionLocal()
    try:
        workflow = __import__("app.models.workflow", fromlist=["AnalysisTask", "Report"])
        after_tasks = session.query(workflow.AnalysisTask).count()
        after_reports = session.query(workflow.Report).count()
    finally:
        session.close()
    assert (after_tasks, after_reports) == (before_tasks, before_reports)


def test_问答不改变用户数据(client, auth):
    """确认问答没有副作用（只读）——这条防止以后有人在问答里"顺手"写点东西。"""
    session = SessionLocal()
    try:
        before = session.query(User).count()
    finally:
        session.close()

    ask(client, auth, "应用商店评论里主要吐槽什么？")

    session = SessionLocal()
    try:
        assert session.query(User).count() == before
    finally:
        session.close()

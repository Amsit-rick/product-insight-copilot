"""集成测试：失败重试、取消、断点恢复与报告版本管理。"""

from __future__ import annotations

FUTURE_QUESTION = "3.2.0 发布后留存下降的归因分析"


def test_invalid_request_fails_fast_and_can_be_retried(client, auth):
    """时间范围超出可用窗口：任务以明确错误码失败，并支持重试。"""
    resp = client.post(
        "/api/v1/tasks",
        headers=auth["pm"],
        json={
            "query": FUTURE_QUESTION,
            "date_from": "2027-01-01",
            "date_to": "2027-01-31",
        },
    )
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["id"]

    detail = client.get(f"/api/v1/tasks/{task_id}", headers=auth["pm"]).json()
    assert detail["status"] == "failed"
    assert detail["error"]["code"] == "invalid_request"

    retried = client.post(f"/api/v1/tasks/{task_id}/retry", headers=auth["pm"], json={})
    assert retried.status_code == 200
    body = retried.json()
    assert body["status"] == "failed"
    assert body["retry_count"] == 1, "重试次数必须累计"

    events = client.get(f"/api/v1/tasks/{task_id}/events", headers=auth["pm"]).json()
    assert any(item["node"] == "retry" for item in events["items"])


def test_cancel_takes_effect_at_node_boundary(client, auth):
    """取消请求在下一个节点入口生效，任务收敛为 cancelled。"""
    from app.db.session import SessionLocal
    from app.models.identity import User
    from app.services.task_service import TaskService
    from app.services.workflow_runner import get_runner

    session = SessionLocal()
    try:
        user = session.query(User).filter(User.username == "pm").first()
        task, _ = TaskService(session).create(
            user=user,
            query="Android 3.2.0 留存下降归因",
            filters={"platform": "Android", "app_version": "3.2.0"},
        )
        session.commit()
        task_id = task.id
    finally:
        session.close()

    cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=auth["pm"])
    assert cancelled.status_code == 200

    get_runner().run(task_id)  # 执行时才检查取消标记
    detail = client.get(f"/api/v1/tasks/{task_id}", headers=auth["pm"]).json()
    assert detail["status"] == "cancelled"

    # 已结束的任务不能再次取消
    again = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=auth["pm"])
    assert again.status_code == 409


def test_reject_and_modify_produce_traceable_reports(client, auth):
    """驳回与人工修改都必须留痕：结论保留、审核状态与 diff 可查。"""
    from app.db.session import SessionLocal
    from app.models.identity import User
    from app.services.task_service import TaskService
    from app.services.workflow_runner import get_runner

    session = SessionLocal()
    user = session.query(User).filter(User.username == "pm").first()
    task, _ = TaskService(session).create(
        user=user,
        query=(
            "Android 端 3.2.0 发布后次日留存下降，哪些用户受到影响？"
            "版本变更与用户反馈是否支持版本相关性？"
        ),
        filters={
            "platform": "Android",
            "app_version": "3.2.0",
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
            "knowledge_bases": ["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
            "metrics": ["next_day_retention", "crash_rate"],
            "force_human_review": True,
        },
    )
    session.commit()
    task_id = task.id
    session.close()

    get_runner().run(task_id)
    detail = client.get(f"/api/v1/tasks/{task_id}", headers=auth["pm"]).json()
    assert detail["status"] == "waiting_human", "强制人工审核的任务必须挂起"

    modified = client.post(
        f"/api/v1/tasks/{task_id}/approve",
        headers=auth["reviewer"],
        json={
            "action": "modify",
            "comment": "修正优先级与摘要后放行",
            "modifications": {"priority": "P0", "summary": "【人工修订】留存下降需立即验证版本回滚。"},
        },
    )
    assert modified.status_code == 200
    assert modified.json()["status"] == "completed"

    report = client.get(f"/api/v1/tasks/{task_id}/report", headers=auth["pm"]).json()
    assert report["approval_status"] == "modified"
    assert report["priority"] == "P0"
    assert "人工修改后通过" in report["summary"]
    assert report["facts"], "人工修改不得删除原有证据与结论"

    approvals = client.get("/api/v1/approvals?status=modified", headers=auth["reviewer"]).json()
    current = [item for item in approvals["items"] if item["task_id"] == task_id]
    assert current, "审核记录必须带 modified 状态"
    assert current[0]["diff"]["decision"]["modifications"]["priority"] == "P0"


def test_stale_running_task_is_reconciled_on_startup(client, auth):
    """进程中断遗留的 running 任务必须被标记为中断失败，并能重试。

    场景复现：工作流跑在进程内线程池里，进程被杀后任务不会自己结束——
    如果不做启动清理，前端会永远显示"执行中"。
    """
    from app.db.session import SessionLocal
    from app.models.identity import User
    from app.models.workflow import AnalysisTask
    from app.services.task_service import TaskService, reconcile_stale_tasks

    session = SessionLocal()
    try:
        user = session.query(User).filter(User.username == "pm").first()
        task, _ = TaskService(session).create(user=user, query="中断遗留任务校验", filters={})
        # 直接把状态改成 running，模拟"进程在跑这个任务时被杀"
        task.status = "running"
        task.current_node = "merge_evidence"
        session.commit()
        task_id = task.id

        interrupted = reconcile_stale_tasks(session, reason="集成测试模拟重启")
        session.commit()
        assert task_id in interrupted

        task = session.get(AnalysisTask, task_id)
        assert task.status == "failed"
        assert task.error["code"] == "interrupted"
        assert "重试" in task.error["message"]
    finally:
        session.close()

    # 前端能看到的错误与事件
    detail = client.get(f"/api/v1/tasks/{task_id}", headers=auth["pm"]).json()
    assert detail["status"] == "failed"
    assert detail["error"]["code"] == "interrupted"

    events = client.get(f"/api/v1/tasks/{task_id}/events", headers=auth["pm"]).json()
    assert any(item["node"] == "startup_reconcile" for item in events["items"])

    # 清理后的任务可以正常重试
    retried = client.post(f"/api/v1/tasks/{task_id}/retry", headers=auth["pm"], json={})
    assert retried.status_code == 200
    assert retried.json()["retry_count"] == 1


def test_report_versions_increment_on_rerun(client, auth):
    """同一任务重复生成报告时版本号递增，历史版本保留。"""
    from app.db.session import SessionLocal
    from app.models.identity import User
    from app.services.report_service import persist_report
    from app.services.task_service import TaskService

    session = SessionLocal()
    try:
        user = session.query(User).filter(User.username == "pm").first()
        task, _ = TaskService(session).create(user=user, query="版本管理校验", filters={})
        session.commit()
        payload = {
            "summary": "测试摘要",
            "facts": [{"id": "fact-1", "type": "fact", "text": "事实", "evidence_refs": []}],
            "evidence": [],
            "priority": "P2",
            "confidence": 0.5,
        }
        first = persist_report(
            session,
            task_id=task.id,
            tenant_id=task.tenant_id,
            report=payload,
            evidence=[],
            created_by=user.id,
        )
        session.commit()
        second = persist_report(
            session,
            task_id=task.id,
            tenant_id=task.tenant_id,
            report=payload,
            evidence=[],
            created_by=user.id,
        )
        session.commit()
        assert (first.version, second.version) == (1, 2)
    finally:
        session.close()

    listing = client.get(f"/api/v1/reports?task_id={task.id}", headers=auth["pm"]).json()
    assert listing["total"] == 2
    assert [item["version"] for item in listing["items"]] == [2, 1]

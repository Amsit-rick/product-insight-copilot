"""An API startup must never invalidate work owned by an independent worker."""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.session import SessionLocal
from app.models.identity import User
from app.models.workflow import AnalysisTask
from app.services.task_service import TaskService


@pytest.mark.parametrize("status", ["pending", "running"])
def test_queue_task_survives_api_lifespan_restart(client, monkeypatch, status):
    import app.main as main

    settings = get_settings().model_copy(update={
        "task_execution_backend": "queue", "task_run_inline": False,
        "worker_embedded": False, "auto_seed": False,
    })
    with SessionLocal() as session:
        user = session.query(User).filter_by(username="pm").one()
        task, _ = TaskService(session).create(user=user, query="队列重启验证", filters={})
        task.status = status
        task_id = task.id
        session.commit()
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    try:
        with TestClient(main.create_app()) as restarted:
            assert restarted.get("/health/live").status_code == 200
        with SessionLocal() as session:
            task = session.get(AnalysisTask, task_id)
            assert task.status == status
            assert task.error is None
    finally:
        with SessionLocal() as session:
            task = session.get(AnalysisTask, task_id)
            task.status = "cancelled"
            session.commit()

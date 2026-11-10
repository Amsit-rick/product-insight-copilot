"""Verify queue and human-interrupt recovery across two actual Python processes.

Uses a fresh temporary DB, seeded synthetic evidence and stub model only. Never
calls a paid model or a business endpoint. This proves same-host SQLite recovery,
not cross-host failover or interruption in the middle of a model call.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from scripts.local_integration import integration_environment  # noqa: E402

PREFIX = "RECOVERY_RESULT="
QUERY = "Android 端 3.2.0 发布后次日留存下降，原因是什么？"
FILTERS = {
    "product": "星野社区", "platform": "Android", "app_version": "3.2.0",
    "date_from": "2026-08-01", "date_to": "2026-08-31",
    "knowledge_bases": ["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
}


def _phase(phase: str, task_id: str | None, pending_id: str | None) -> dict:
    from app.db.seed import init_db, seed_all
    from app.db.session import SessionLocal
    from app.models.identity import User
    from app.models.workflow import AnalysisEvent, AnalysisTask
    from app.services.task_queue import KIND_RESUME, SqlTaskQueue
    from app.services.task_service import TaskService
    from app.services.workflow_runner import get_runner
    from app.worker import Worker

    queue = SqlTaskQueue()
    if phase == "before":
        init_db()
        seed_all()
        with SessionLocal() as session:
            user = session.query(User).filter_by(username="pm").one()
            task, _ = TaskService(session).create(user=user, query=QUERY, filters=FILTERS)
            task_id = task.id
            session.commit()
        queue.enqueue(task_id)
        assert Worker().run_once() == 1
        with SessionLocal() as session:
            assert session.get(AnalysisTask, task_id).status == "waiting_human"
            user = session.query(User).filter_by(username="pm").one()
            pending, _ = TaskService(session).create(user=user, query=QUERY, filters=FILTERS)
            pending_id = pending.id
            session.commit()
        queue.enqueue(pending_id)
        checkpoint = get_runner().checkpointer.get_tuple({"configurable": {"thread_id": task_id}})
        assert checkpoint is not None
        return {"pid": os.getpid(), "task_id": task_id, "pending_id": pending_id,
                "checkpoint_written": True, "waiting_human": True}

    from fastapi.testclient import TestClient
    from app.main import create_app

    # Run the real API startup hook in the new process, before the worker starts.
    with TestClient(create_app()) as client:
        assert client.get("/health/ready").status_code == 200
        with SessionLocal() as session:
            assert session.get(AnalysisTask, pending_id).status == "pending"
            assert session.get(AnalysisTask, task_id).status == "waiting_human"
            start_count = session.query(AnalysisEvent).filter_by(
                task_id=task_id, node="runner", message="工作流开始执行",
            ).count()
        snapshot = get_runner().graph.get_state({"configurable": {"thread_id": task_id}})
        assert snapshot.next, "new process must see the persisted human interrupt"
        assert Worker().run_once() == 1  # Pending work left by the exited process.
        queue.enqueue(task_id, kind=KIND_RESUME, payload={
            "action": "approve", "comment": "隔离进程恢复验证", "reviewer_id": None,
        })
        assert Worker().run_once() == 1
        with SessionLocal() as session:
            task = session.get(AnalysisTask, task_id)
            assert task.status == "completed"
            assert task.resumed_from_checkpoint is True
            assert task.resume_count == 1
            assert session.get(AnalysisTask, pending_id).status == "waiting_human"
            assert session.query(AnalysisEvent).filter_by(
                task_id=task_id, node="runner", message="工作流开始执行",
            ).count() == start_count
    return {"pid": os.getpid(), "pending_preserved_on_api_restart": True,
            "pending_executed_by_new_worker": True, "resumed_from_checkpoint": True,
            "completed_after_review": True, "initial_workflow_not_restarted": True}


def verify() -> dict:
    with tempfile.TemporaryDirectory(prefix="recovery-", dir=BACKEND / ".tmp") as directory:
        env = integration_environment(Path(directory), 8091)
        env.update({
            "LLM_PROVIDER": "stub", "LLM_API_KEY": "",
            "AGENT_MODEL_OVERRIDES": "{}", "LLM_FALLBACK_MODELS": "[]",
            "EMBEDDING_PROVIDER": "stub", "EMBEDDING_API_KEY": "",
            "RERANKER_ENDPOINT": "", "ALERT_WEBHOOK_ENABLED": "false",
            "REFLECTION_ENABLED": "false", "METRIC_SOURCE_HTTP_ENABLED": "false",
            "PYTHONIOENCODING": "utf-8",
        })

        def child(phase: str, *ids: str) -> dict:
            args = [sys.executable, str(Path(__file__).resolve()), "--phase", phase]
            if ids:
                args += ["--task-id", ids[0], "--pending-id", ids[1]]
            result = subprocess.run(args, cwd=BACKEND, env=env, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=90)
            if result.returncode:
                # Stub-only isolated child logs contain no configured model credentials.
                raise RuntimeError(f"recovery {phase} failed:\n{result.stderr[-3000:]}\n{result.stdout[-1500:]}")
            return json.loads(next(line[len(PREFIX):] for line in result.stdout.splitlines()
                                   if line.startswith(PREFIX)))

        before = child("before")
        after = child("after", before["task_id"], before["pending_id"])
        assert before["pid"] != after["pid"]
        return {
            "passed": True, "model": "stub", "scope": "same_host_sqlite",
            "different_processes": True,
            "checks": {key: value for key, value in {**before, **after}.items()
                       if isinstance(value, bool)},
            "production_ready": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("before", "after"))
    parser.add_argument("--task-id")
    parser.add_argument("--pending-id")
    args = parser.parse_args()
    if args.phase:
        print(PREFIX + json.dumps(_phase(args.phase, args.task_id, args.pending_id)))
    else:
        (BACKEND / ".tmp").mkdir(exist_ok=True)
        print(json.dumps(verify(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

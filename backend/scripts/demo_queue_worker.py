"""多副本执行链路的实跑演示：队列 → worker → 任务完成（离线、不花钱）。

在一座临时库里完整走一遍生产才有的路径：

1. 以 `queue` 后端建任务：接口只入队，**接口返回时任务还没跑**（状态 pending）；
2. 起一个 worker 领取并执行 → 任务完成、队列项 ack；
3. 同一个队列项不会被第二个 worker 重复领取（多副本下最关键的性质）；
4. 模拟 worker 崩溃（租约过期）→ 任务被回收并由健康 worker 跑完；
5. 打印队列统计与 checkpointer 状态（`memory` / `sqlite` 决定恢复能不能跨副本）。

用法（在 backend 目录下）::

    ..\\.venv\\Scripts\\python.exe scripts\\demo_queue_worker.py
    ..\\.venv\\Scripts\\python.exe scripts\\demo_queue_worker.py --checkpointer sqlite
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
TMP_DB = BACKEND / ".tmp" / "queue_worker_demo.db"

# 必须在导入 app.* 之前指定数据库，否则会用开发库（脚本绝不能碰开发数据）
if TMP_DB.exists():
    TMP_DB.unlink()
TMP_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{TMP_DB.as_posix()}"
os.environ["LLM_PROVIDER"] = "stub"
os.environ["EMBEDDING_PROVIDER"] = "stub"
os.environ["LLM_API_KEY"] = ""
os.environ["AGENT_MODEL_OVERRIDES"] = "{}"
os.environ["TASK_EXECUTION_BACKEND"] = "queue"
os.environ["WORKER_EMBEDDED"] = "false"
os.environ["AUTO_INIT_DB"] = "true"

sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402
from app.db.seed import init_db, seed_all  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.graph.checkpointer import build_checkpointer, describe_checkpointer, reset_checkpointer  # noqa: E402
from app.models.base import utcnow  # noqa: E402
from app.models.identity import User  # noqa: E402
from app.models.queue import TaskQueueItem  # noqa: E402
from app.models.workflow import AnalysisTask  # noqa: E402
from app.services.task_dispatch import dispatch_task  # noqa: E402
from app.services.task_queue import SqlTaskQueue  # noqa: E402
from app.services.task_service import TaskService  # noqa: E402
from app.worker import Worker  # noqa: E402


def status_of(task_id: str) -> str:
    session = SessionLocal()
    try:
        return session.get(AnalysisTask, task_id).status
    finally:
        session.close()


def item_of(task_id: str) -> TaskQueueItem | None:
    session = SessionLocal()
    try:
        return (
            session.query(TaskQueueItem)
            .filter(TaskQueueItem.task_id == task_id)
            .order_by(TaskQueueItem.created_at.desc())
            .first()
        )
    finally:
        session.close()


def create_task() -> str:
    session = SessionLocal()
    try:
        pm = session.query(User).filter(User.username == "pm").first()
        task, _ = TaskService(session).create(
            user=pm,
            query="Android 端 3.2.0 发布后次日留存下降，原因是什么？",
            filters={
                "product": "星野社区",
                "platform": "Android",
                "app_version": "3.2.0",
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
                "knowledge_bases": ["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
            },
        )
        session.commit()
        return task.id
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="队列 + worker 实跑演示")
    parser.add_argument("--checkpointer", default="memory", choices=["memory", "sqlite"])
    args = parser.parse_args(argv)

    if args.checkpointer == "sqlite":
        os.environ["CHECKPOINTER_BACKEND"] = "sqlite"
        os.environ["CHECKPOINTER_SQLITE_PATH"] = str(BACKEND / ".tmp" / "queue_worker_demo_ckpt.db")
    get_settings.cache_clear()
    settings = get_settings()
    reset_checkpointer()
    build_checkpointer(settings)

    session = SessionLocal()
    try:
        print("=== 准备演示数据（临时库，不碰开发库）===")
        init_db()  # 空库先建表（服务启动时由 lifespan 负责，脚本要自己来）
        seed_all(session=session, with_eval_cases=False)
    finally:
        session.close()

    queue = SqlTaskQueue(settings)

    print("\n=== ① 以 queue 后端建任务：只入队，不执行 ===")
    task_id = create_task()
    dispatch_task(task_id, settings=settings)
    item = item_of(task_id)
    print(f"  任务 {task_id[:8]} 状态={status_of(task_id)}  队列项状态={item.status} attempts={item.attempts}")
    print("  说明：接口返回时任务还是 pending —— 执行权在 worker 手上，API 可以随便重启")

    print("\n=== ② 一个 worker 领取并跑完 ===")
    worker = Worker(settings=settings, queue=queue, worker_id="worker-1", concurrency=1)
    handled = worker.run_once()
    item = item_of(task_id)
    print(f"  本轮处理 {handled} 个；任务状态={status_of(task_id)}  队列项={item.status} by={item.claimed_by}")
    print(f"  队列统计={queue.stats()}")

    print("\n=== ③ 同一个队列项不会被第二个 worker 重复执行 ===")
    second = Worker(settings=settings, queue=queue, worker_id="worker-2", concurrency=1)
    print(f"  worker-2 本轮处理 {second.run_once()} 个（应为 0）")

    print("\n=== ④ worker 崩溃：租约过期后任务被回收重跑 ===")
    crashed_id = create_task()
    dispatch_task(crashed_id, settings=settings)
    claimed = queue.claim("worker-crashed", limit=1)
    print(f'  worker-crashed 领走 {len(claimed)} 个后"装死"（不再 ack）')
    session = SessionLocal()
    try:
        row = session.get(TaskQueueItem, claimed[0].id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()
    healthy = Worker(settings=settings, queue=queue, worker_id="worker-healthy", concurrency=1)
    handled = healthy.run_once()
    item = item_of(crashed_id)
    print(f"  健康 worker 处理 {handled} 个；任务状态={status_of(crashed_id)} 队列项={item.status} attempts={item.attempts}")

    print("\n=== ⑤ 断点恢复能力（决定多副本下人工审核能否恢复）===")
    print(f"  checkpointer={describe_checkpointer(settings)}")
    if not describe_checkpointer(settings).get("shared"):
        print("  提示：memory 只能单副本；多副本请设 CHECKPOINTER_BACKEND=sqlite（同机）或 postgres")
    print(f"  最终队列统计={queue.stats()}")

    print("\n=== 结论 ===")
    print("  · API 只入队 → 可多副本、可滚动重启")
    print("  · 条件更新保证同一任务只被一个 worker 领取（不会重复花钱、重复写库）")
    print("  · 租约 + 回收让 worker 崩溃不致任务永久卡死")
    print('  · checkpointer 是否共享决定「中断后恢复」能不能换副本')

    # 清理临时库：先释放连接池，否则 Windows 上文件仍被占用（删不掉会报错退出码非 0）
    from app.db.session import engine

    engine.dispose()
    try:
        if TMP_DB.exists():
            TMP_DB.unlink()
    except PermissionError:
        print(f"  （临时库 {TMP_DB.name} 仍被占用，可忽略）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

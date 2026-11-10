"""任务分发：把"建任务/重试/审核恢复"统一接到配置好的执行后端上。

三种后端（`TASK_EXECUTION_BACKEND`）：

| 后端 | 行为 | 适用 |
|---|---|---|
| `inline` | 接口内同步执行，返回时任务已跑完 | 测试、单机调试（也兼容旧的 `TASK_RUN_INLINE=true`） |
| `threadpool` | 丢进本进程线程池 | 单副本部署（默认） |
| `queue` | 写入任务队列，由 worker 进程领取 | 多副本、需要独立扩缩容与崩溃自愈 |

放在一个函数里而不是散在三处路由，是因为这三条路径（新建 / 重试 / 审核恢复）
必须行为一致——只改一处就会出现"新建走队列、重试还在本进程跑"这种难查的错位。
"""

from __future__ import annotations

from app.config import Settings, get_settings
from app.logging_utils import get_logger
from app.services.task_queue import KIND_RESUME, KIND_RUN, get_task_queue

logger = get_logger(__name__)


def resolve_backend(settings: Settings | None = None) -> str:
    """实际生效的执行后端（`task_run_inline` 是旧开关，优先级最高）。"""
    settings = settings or get_settings()
    if settings.task_run_inline:
        return "inline"
    backend = (settings.task_execution_backend or "threadpool").strip().lower()
    return backend if backend in {"inline", "threadpool", "queue"} else "threadpool"


def dispatch_task(
    task_id: str,
    *,
    kind: str = KIND_RUN,
    payload: dict | None = None,
    settings: Settings | None = None,
) -> str:
    """按配置分发任务，返回实际使用的后端名（便于测试与日志断言）。"""
    settings = settings or get_settings()
    backend = resolve_backend(settings)

    if backend == "inline":
        from app.services.workflow_runner import get_runner

        runner = get_runner()
        if kind == KIND_RESUME:
            runner.resume(task_id, payload or {})
        else:
            runner.run(task_id)
        return backend

    if backend == "queue":
        item_id = get_task_queue(settings).enqueue(task_id, kind=kind, payload=payload or {})
        logger.info(
            "任务 %s（%s）已入队%s", task_id, kind, "" if item_id else "（已存在，跳过重复入队）"
        )
        return backend

    from app.services.workflow_runner import get_runner

    runner = get_runner()
    if kind == KIND_RESUME:
        runner.submit_resume(task_id, payload or {})
    else:
        runner.submit(task_id)
    return backend


__all__ = ["KIND_RESUME", "KIND_RUN", "dispatch_task", "resolve_backend"]

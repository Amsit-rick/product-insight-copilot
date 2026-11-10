"""独立 worker 进程：从任务队列领取任务并执行。

为什么要有它：API 进程里跑线程池时，任务只能落在"收到请求的那个副本"上，
副本被回收（部署、扩缩容、OOM）任务就跟着中断。把执行搬到独立 worker 后：

- API 只负责入队 → 可以放心多副本、随便滚动重启；
- worker 可以独立扩缩容（`--concurrency`），与 API 的资源曲线分开；
- worker 崩了也不会丢任务：租约过期后由 `reclaim_expired()` 放回队列。

用法::

    python -m app.worker                     # 常驻，按配置的并发与轮询间隔
    python -m app.worker --concurrency 4     # 覆盖并发
    python -m app.worker --once              # 只跑一轮（用于脚本化验证与测试）
    python -m app.worker --drain             # 处理完当前可见任务就退出

`WORKER_EMBEDDED=true` 时 API 进程会内嵌一个同样的 worker（本地开发省一个进程），
生产建议关掉：让 API 与 worker 的故障域分开。
"""

from __future__ import annotations

import argparse
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import inspect

from app.config import Settings, get_settings
from app.db.session import engine
from app.logging_utils import get_logger
from app.services.task_queue import KIND_RESUME, ClaimedItem, SqlTaskQueue, default_worker_id
from app.services.worker_heartbeat import WorkerHeartbeat

logger = get_logger(__name__)


def queue_schema_ready() -> bool:
    """API 初始化表结构后，worker 才能开始轮询队列。"""
    try:
        return bool(inspect(engine).has_table("task_queue_items"))
    except Exception:
        return False


def wait_for_queue_schema(
    *, timeout_seconds: float = 120.0, poll_interval_seconds: float = 1.0
) -> None:
    """等待 API/迁移把队列表建好，避免冷启动时 worker 因 UndefinedTable 崩溃。"""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while not queue_schema_ready():
        if time.monotonic() >= deadline:
            raise RuntimeError("等待 task_queue_items 表就绪超时，请先完成数据库初始化/迁移")
        time.sleep(max(0.05, poll_interval_seconds))


class Worker:
    """按队列领取→执行→确认的循环。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        queue: SqlTaskQueue | None = None,
        worker_id: str | None = None,
        concurrency: int | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.queue = queue or SqlTaskQueue(self.settings)
        self.worker_id = worker_id or self.settings.worker_id or default_worker_id()
        self.concurrency = concurrency or self.settings.worker_concurrency
        self._stop = threading.Event()
        self.processed = 0
        self.failed = 0

    # ------------------------------------------------------------------ 单轮
    def run_once(self) -> int:
        """回收过期租约 → 领取 → 执行 → 确认。返回本轮处理的任务数。"""
        reclaimed = self.queue.reclaim_expired()
        if reclaimed:
            logger.info("回收了 %s 条租约过期的任务", reclaimed)

        items = self.queue.claim(self.worker_id, limit=self.concurrency)
        if not items:
            return 0

        with ThreadPoolExecutor(
            max_workers=max(1, min(self.concurrency, len(items))), thread_name_prefix="worker"
        ) as pool:
            list(pool.map(self._handle, items))
        return len(items)

    def _handle(self, item: ClaimedItem) -> None:
        from app.services.workflow_runner import get_runner

        started = time.perf_counter()
        try:
            runner = get_runner()
            if item.kind == KIND_RESUME:
                runner.resume(item.task_id, item.payload or {})
            else:
                runner.run(item.task_id)
        except Exception as exc:  # 执行失败：交给队列决定重试还是判死
            self.failed += 1
            next_state = self.queue.nack(
                item.id,
                self.worker_id,
                f"{type(exc).__name__}: {exc}",
                retry_delay_seconds=float(self.settings.worker_poll_interval_seconds) * 5,
            )
            logger.exception("任务 %s 执行失败（第 %s 次，队列状态 → %s）", item.task_id, item.attempts, next_state)
            return
        self.processed += 1
        self.queue.ack(item.id, self.worker_id)
        logger.info(
            "任务 %s 执行完成（第 %s 次尝试，耗时 %.1fs）",
            item.task_id,
            item.attempts,
            time.perf_counter() - started,
        )

    # ------------------------------------------------------------------ 常驻
    def run_forever(self, *, max_rounds: int | None = None) -> None:
        """常驻循环。`max_rounds` 仅用于测试/脚本化运行。"""
        rounds = 0
        heartbeat = WorkerHeartbeat(
            self.settings.worker_heartbeat_path,
            interval_seconds=self.settings.worker_heartbeat_interval_seconds,
        )
        heartbeat.start()
        logger.info(
            "worker 启动：id=%s 并发=%s 轮询=%.1fs 租约=%ss",
            self.worker_id,
            self.concurrency,
            self.settings.worker_poll_interval_seconds,
            self.settings.worker_lease_seconds,
        )
        try:
            while not self._stop.is_set():
                handled = self.run_once()
                rounds += 1
                if max_rounds is not None and rounds >= max_rounds:
                    break
                if handled == 0:
                    self._stop.wait(self.settings.worker_poll_interval_seconds)
        finally:
            heartbeat.stop()
            logger.info("worker 退出：处理 %s 个、失败 %s 个", self.processed, self.failed)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ 内嵌
    def start_background(self) -> threading.Thread:
        """在 API 进程内以守护线程运行（WORKER_EMBEDDED=true 时使用）。"""
        thread = threading.Thread(target=self.run_forever, name="embedded-worker", daemon=True)
        thread.start()
        return thread


_worker: Worker | None = None
_worker_lock = threading.Lock()


def get_embedded_worker(settings: Settings | None = None) -> Worker:
    """进程内共享的 worker 实例（API 内嵌模式）。"""
    global _worker
    settings = settings or get_settings()
    with _worker_lock:
        if _worker is None:
            _worker = Worker(settings=settings, worker_id=settings.worker_id or default_worker_id())
        return _worker


def reset_embedded_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.stop()
        _worker = None


def start_embedded_worker(settings: Settings | None = None) -> Any:
    """按配置决定是否内嵌 worker；返回线程对象或 None。"""
    settings = settings or get_settings()
    if settings.task_execution_backend != "queue" or not settings.worker_embedded:
        return None
    return get_embedded_worker(settings).start_background()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="电商售后工单智能处理系统任务 worker")
    parser.add_argument("--concurrency", type=int, default=None, help="并发处理的任务数")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出")
    parser.add_argument("--drain", action="store_true", help="处理完当前可见任务后退出")
    parser.add_argument("--rounds", type=int, default=None, help="最多循环轮数（脚本化运行用）")
    args = parser.parse_args(argv)

    wait_for_queue_schema()
    worker = Worker(concurrency=args.concurrency)

    def _handle_signal(signum, _frame):  # pragma: no cover - 信号处理不便自动化测试
        logger.info("收到信号 %s，准备退出（不再领取新任务）", signum)
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):  # 非主线程/Windows 受限场景
            pass

    if args.once:
        handled = worker.run_once()
        print(f"处理了 {handled} 个任务（成功 {worker.processed} / 失败 {worker.failed}）")
        return 0
    if args.drain:
        while worker.run_once() > 0:
            pass
        print(f"队列已清空：成功 {worker.processed} / 失败 {worker.failed}")
        return 0
    worker.run_forever(max_rounds=args.rounds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

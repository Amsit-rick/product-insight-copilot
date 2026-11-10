"""独立 Worker 的轻量活性心跳。

心跳文件只包含当前 UTC 时间，不含任务、租户或业务数据。Docker 健康检查读取
文件修改时间，因此不会访问数据库，也不会因为上游短暂抖动把存活进程误判为死亡。
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path


def heartbeat_is_fresh(
    path: str | Path,
    *,
    max_age_seconds: float,
    now: float | None = None,
) -> bool:
    """心跳存在且未超过最大年龄时返回 ``True``。"""

    heartbeat = Path(path)
    try:
        modified_at = heartbeat.stat().st_mtime
    except OSError:
        return False
    current = time.time() if now is None else now
    age = current - modified_at
    return 0 <= age <= max_age_seconds


class WorkerHeartbeat:
    """在守护线程中刷新心跳文件，并在退出时清理。"""

    def __init__(self, path: str | Path, *, interval_seconds: float) -> None:
        self.path = Path(path)
        self.interval_seconds = max(0.1, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(datetime.now(UTC).isoformat(), encoding="ascii")

    def _run(self) -> None:
        while not self._stop.is_set():
            self._write()
            self._stop.wait(self.interval_seconds)

    def start(self) -> threading.Thread:
        self._write()
        self._thread = threading.Thread(
            target=self._run,
            name="worker-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


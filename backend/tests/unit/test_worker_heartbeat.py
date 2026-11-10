"""独立 Worker 心跳与健康探针。"""

from __future__ import annotations

import os
import time

from app.services.worker_heartbeat import WorkerHeartbeat, heartbeat_is_fresh


def test_heartbeat_freshness_fails_closed_for_missing_and_stale_files(tmp_path):
    path = tmp_path / "heartbeat"

    assert heartbeat_is_fresh(path, max_age_seconds=10, now=100) is False

    path.write_text("timestamp", encoding="ascii")
    os.utime(path, (80, 80))
    assert heartbeat_is_fresh(path, max_age_seconds=10, now=100) is False

    os.utime(path, (95, 95))
    assert heartbeat_is_fresh(path, max_age_seconds=10, now=100) is True


def test_worker_heartbeat_refreshes_and_cleans_file(tmp_path):
    path = tmp_path / "heartbeat"
    heartbeat = WorkerHeartbeat(path, interval_seconds=0.05)

    heartbeat.start()
    first_mtime = path.stat().st_mtime
    time.sleep(0.08)
    refreshed_mtime = path.stat().st_mtime
    heartbeat.stop()

    assert refreshed_mtime >= first_mtime
    assert not path.exists()

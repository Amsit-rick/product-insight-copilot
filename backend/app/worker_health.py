"""Worker 容器健康检查入口。"""

from __future__ import annotations

from app.config import get_settings
from app.services.worker_heartbeat import heartbeat_is_fresh


def main() -> int:
    settings = get_settings()
    healthy = heartbeat_is_fresh(
        settings.worker_heartbeat_path,
        max_age_seconds=settings.worker_heartbeat_max_age_seconds,
    )
    print("worker_heartbeat=" + ("fresh" if healthy else "stale_or_missing"))
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())


"""Checkpointer 工厂：让"断点恢复"能跨进程、跨副本。

为什么必须可配置：默认的 `InMemorySaver` 把中断状态存在**进程内存**里，于是
"人工审核后恢复"在两种情况下会失效：

- 进程重启（部署、崩溃、扩容回收）→ checkpoint 没了，只能靠落库摘要降级重建报告；
- 多副本：提交审核请求的副本如果没有这个任务的 checkpoint，恢复就走降级路径。

所以生产上要么用共享的 SQLite（同机多进程），要么用 Postgres（真正多副本）。
这里把选择做成配置，并把"当前用的是哪种"暴露给 `/health`，避免运维"以为是共享的"。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.logging_utils import get_logger

logger = get_logger(__name__)

BACKENDS = ("memory", "sqlite", "postgres")

_lock = threading.Lock()
_saver: Any | None = None
_saver_context: Any | None = None
_description: dict[str, Any] = {}


class CheckpointerConfigError(RuntimeError):
    """配置了不可用的 checkpointer 后端：启动时就失败，别等到恢复任务时才发现。"""


def _sqlite_path(settings: Settings) -> Path:
    if settings.checkpointer_sqlite_path:
        return Path(settings.checkpointer_sqlite_path)
    # 默认放在数据库同目录（SQLite 模式下就是项目 data 目录）
    url = settings.database_url or ""
    if url.startswith("sqlite:///"):
        base = Path(url.replace("sqlite:///", "", 1)).parent
    else:
        base = Path("data")
    return base / "checkpoints.db"


def build_checkpointer(settings: Settings | None = None) -> Any:
    """按配置创建 checkpointer（进程内单例）。"""
    global _saver, _saver_context, _description
    settings = settings or get_settings()
    with _lock:
        if _saver is not None:
            return _saver
        backend = (settings.checkpointer_backend or "memory").strip().lower()
        if backend not in BACKENDS:
            raise CheckpointerConfigError(
                f"CHECKPOINTER_BACKEND={backend!r} 不支持，可选：{'、'.join(BACKENDS)}"
            )

        if backend == "memory":
            from langgraph.checkpoint.memory import InMemorySaver

            _saver = InMemorySaver()
            _description = {
                "backend": "memory",
                "shared": False,
                "note": "进程内内存：重启或换副本后无法恢复中断，会走「用落库摘要降级重建报告」的兜底路径",
            }
            logger.warning("Checkpointer 使用进程内内存：中断恢复不能跨进程（生产请用 sqlite / postgres）")
            return _saver

        if backend == "sqlite":
            try:
                from langgraph.checkpoint.sqlite import SqliteSaver
            except ImportError as exc:  # pragma: no cover - 依赖缺失时的提示
                raise CheckpointerConfigError(
                    "缺少 langgraph-checkpoint-sqlite 依赖，无法使用 sqlite checkpointer"
                ) from exc
            path = _sqlite_path(settings)
            path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False：工作流跑在线程池里，连接会被多个线程用到；
            # SQLite 侧由 WAL + busy_timeout 兜住并发写
            conn = sqlite3.connect(str(path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            _saver = SqliteSaver(conn)
            _description = {
                "backend": "sqlite",
                "shared": True,
                "path": str(path),
                "note": "同机多进程共享；跨主机多副本请改用 postgres",
            }
            logger.info("Checkpointer 使用 SQLite：%s（中断恢复可跨进程）", path)
            return _saver

        # postgres：真正多副本共享
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:
            raise CheckpointerConfigError(
                "CHECKPOINTER_BACKEND=postgres 需要安装 langgraph-checkpoint-postgres"
                "（pip install langgraph-checkpoint-postgres psycopg[binary]）；"
                "如果暂时装不了，请改用 sqlite（同机多进程）或 memory（单副本）"
            ) from exc
        url = settings.checkpointer_postgres_url or settings.database_url
        if not url or not str(url).startswith("postgres"):
            raise CheckpointerConfigError(
                "postgres checkpointer 需要一个 postgres 连接串："
                "设置 CHECKPOINTER_POSTGRES_URL 或 DATABASE_URL"
            )
        context = PostgresSaver.from_conn_string(str(url))
        try:
            saver = context.__enter__()
            saver.setup()
        except Exception:
            context.__exit__(*__import__("sys").exc_info())
            raise
        # 必须保存 context 本身，而不只是 __enter__ 返回的 saver。
        # 否则临时 context 被回收时会关闭 saver 仍在使用的数据库连接。
        _saver_context = context
        _saver = saver
        _description = {"backend": "postgres", "shared": True, "note": "多副本共享，生产推荐"}
        logger.info("Checkpointer 使用 Postgres（多副本共享）")
        return _saver


def describe_checkpointer(settings: Settings | None = None) -> dict[str, Any]:
    """当前 checkpointer 的描述（供 /health 展示，不含任何密钥）。"""
    settings = settings or get_settings()
    if _description:
        return dict(_description)
    return {
        "backend": (settings.checkpointer_backend or "memory"),
        "shared": (settings.checkpointer_backend or "memory") != "memory",
        "note": "尚未初始化（进程启动后首次执行工作流时建立）",
    }


def reset_checkpointer() -> None:
    """测试用：清掉单例（下一次调用会按配置重建）。"""
    global _saver, _saver_context, _description
    with _lock:
        context = _saver_context
        _saver_context = None
        _saver = None
        _description = {}
        if context is not None:
            context.__exit__(None, None, None)


__all__ = [
    "BACKENDS",
    "CheckpointerConfigError",
    "build_checkpointer",
    "describe_checkpointer",
    "reset_checkpointer",
]

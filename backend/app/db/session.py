"""数据库连接与会话管理。

同步 Session + 线程内执行：工作流与 Agent 调用是阻塞 IO，统一放在后台线程执行，
避免异步驱动（asyncpg / aiosqlite）与 Windows 环境的兼容问题。

SQLite 说明（本地零依赖模式的关键一环）：
工作流会在多个线程里并发写入（4 个 Agent 并行 + 节点事件 + 审计），
而 SQLite 默认只允许一个写者、且读事务会挡住写提交。
因此这里对 SQLite 统一开启：

- `journal_mode=WAL`：读写不互相阻塞（读事务不再挡住写提交）；
- `busy_timeout=30s`：写锁竞争时等待而不是立刻报 "database is locked"；
- `synchronous=NORMAL`：WAL 下的常规安全档位，兼顾性能；
- `foreign_keys=ON`：与 Postgres 保持一致的引用完整性语义。

即便如此，**长写事务仍会致命**：详见下方 `write_scope()` 的说明。
"""

from __future__ import annotations

import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings

SQLITE_BUSY_TIMEOUT_MS = 30_000


def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
    """仅为 SQLite 连接设置 PRAGMA（在引擎实例上注册，不影响其他方言）。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def build_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    url = settings.database_url
    kwargs: dict[str, Any] = {
        "echo": settings.db_echo,
        "future": True,
        "pool_pre_ping": True,
    }
    if url.startswith("sqlite"):
        # SQLite 在多线程下需要关闭同线程检查，并允许排队等待写锁
        kwargs["connect_args"] = {
            "check_same_thread": False,
            "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
        }
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


_settings = get_settings()
engine = build_engine(_settings)
SessionLocal = build_session_factory(engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每个请求一个 Session。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """后台线程 / 脚本使用的会话上下文。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_session() -> Session:
    """供非请求上下文（后台任务）手动管理生命周期。"""
    return SessionLocal()


# --------------------------------------------------------------------------- 写事务
# 为什么需要"短写事务 + 进程内串行"：
# 工作流里 4 个分析 Agent 是**并行**跑的，每个 Agent 都要写自己的 agent_runs / tool_calls /
# 事件行。若用长事务（节点入口 flush 一次、节点结束才提交），每个 Agent 都会把 SQLite 的
# 写锁**横跨整个模型调用**（真实模型下是几秒）。SQLite 的写锁是全局的，且"先读后写"的
# 升级型事务**不会**等 busy_timeout，于是直接 "database is locked" 把任务打成失败——
# stub 模型跑得飞快，所以这个问题只在接上真实模型后才暴露。
#
# 解决办法：所有"记账型"写入都走 `write_scope()`——进程内串行、写完立即提交，
# 写锁只在毫秒级持有；读事务在 WAL 下不阻塞写事务，并行 Agent 的读性能不受影响。
_write_lock = threading.RLock()


@contextmanager
def write_scope(session_factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """短写事务：串行化 + 立即提交，避免长事务长时间占着写锁。

    `session_factory` 可注入：任务队列这类服务在测试里需要指向临时库，
    绝不能因为"写事务走全局 SessionLocal"而把测试数据写进开发库。
    """
    factory = session_factory or SessionLocal
    with _write_lock:
        db = factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


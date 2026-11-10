"""缓存：Redis 优先，不可用时自动降级为进程内缓存。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.logging_utils import get_logger

logger = get_logger(__name__)


class CacheBackend:
    def get(self, key: str) -> Any | None:  # pragma: no cover - 接口
        raise NotImplementedError

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:  # pragma: no cover
        raise NotImplementedError

    def delete(self, key: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def increment(self, key: str, *, ttl: int) -> int:  # pragma: no cover
        raise NotImplementedError


class MemoryCache(CacheBackend):
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expire_at, value = item
            if expire_at and time.time() > expire_at:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        with self._lock:
            self._data[key] = (time.time() + ttl if ttl else 0, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def increment(self, key: str, *, ttl: int) -> int:
        with self._lock:
            current = self.get(key)
            value = int(current or 0) + 1
            self.set(key, value, ttl)
            return value


class RedisCache(CacheBackend):
    """Redis 后端。

    必须设置 **socket 超时**：Redis 不可达时（尤其是被防火墙丢包而不是拒绝连接），
    没有超时的客户端会把请求无限期挂住——缓存本意是加速，绝不能变成"整个服务卡死"。
    """

    def __init__(self, url: str, *, connect_timeout: float = 1.0, socket_timeout: float = 2.0) -> None:
        import redis  # 延迟导入，避免无 Redis 环境启动失败

        self._client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=connect_timeout,
            socket_timeout=socket_timeout,
            retry_on_timeout=False,
            health_check_interval=30,
        )

    def ping(self) -> bool:
        """真正探测可用性（构造 Cache 时用，决定是否降级到进程内缓存）。"""
        return bool(self._client.ping())

    def get(self, key: str) -> Any | None:
        try:
            raw = self._client.get(key)
        except Exception:
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        try:
            self._client.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=ttl)
        except Exception:
            return

    def delete(self, key: str) -> None:
        try:
            self._client.delete(key)
        except Exception:
            return

    def increment(self, key: str, *, ttl: int) -> int:
        # INCR + EXPIRE 放在同一事务中，避免并发丢计数，也避免进程在两条命令间退出留下永久键。
        with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl)
            value, _ = pipe.execute()
        return int(value)


class SqliteCache(CacheBackend):
    """落盘缓存（Redis 不可用时的降级后端）。

    **为什么不再用纯进程内字典**：开发与演示时后端会被反复重启，
    内存缓存每次重启清零 —— 于是"同一个问题问第二遍"也永远不命中，
    命中率长期停在个位数，缓存看起来像坏了一样。落盘之后重启仍然命中，
    多进程（API + worker）也能共享同一份缓存。

    为什么独立一个库文件而不是复用业务库：缓存是**可丢弃数据**，
    不该进业务备份、也不该为它维护迁移；这里 `CREATE TABLE IF NOT EXISTS` 足够。

    为什么不会读到过期数据：
    1. 键里本来就带数据指纹（知识库切片 / 指标导入），数据一变键就变；
    2. 每条记录带 expires_at，过期即视为未命中（TTL 兜底）。
    """

    def __init__(self, path: str) -> None:
        self._path = path
        directory = Path(path).parent
        directory.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cache_entries ("
                " key TEXT PRIMARY KEY,"
                " expires_at REAL NOT NULL,"
                " value TEXT NOT NULL)"
            )

    def get(self, key: str) -> Any | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT expires_at, value FROM cache_entries WHERE key = ?", (key,)
                ).fetchone()
        except Exception:  # pragma: no cover - 磁盘异常一律当作未命中
            return None
        if row is None:
            return None
        expires_at, raw = row
        if expires_at and time.time() > expires_at:
            self.delete(key)
            return None
        try:
            return json.loads(raw)
        except Exception:  # pragma: no cover
            return None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        expires_at = time.time() + ttl if ttl else 0.0
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO cache_entries (key, expires_at, value) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET expires_at = excluded.expires_at, value = excluded.value",
                    (key, expires_at, json.dumps(value, ensure_ascii=False, default=str)),
                )
        except Exception:  # pragma: no cover
            return

    def delete(self, key: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
        except Exception:  # pragma: no cover
            return

    def increment(self, key: str, *, ttl: int) -> int:
        expires_at = time.time() + ttl
        with self._connect() as conn:
            # BEGIN IMMEDIATE 把「读当前值 + 写新值」变成单个跨进程临界区。
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO cache_entries (key, expires_at, value) VALUES (?, ?, '1') "
                "ON CONFLICT(key) DO UPDATE SET "
                "expires_at = excluded.expires_at, "
                "value = CAST(cache_entries.value AS INTEGER) + 1",
                (key, expires_at),
            )
            row = conn.execute(
                "SELECT value FROM cache_entries WHERE key = ?", (key,)
            ).fetchone()
            return int(row[0])

    def prune_expired(self) -> int:
        """清掉过期行（启动时与写入够多次后调用，避免文件无限增长）。"""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM cache_entries WHERE expires_at > 0 AND expires_at < ?",
                    (time.time(),),
                )
                return int(cursor.rowcount or 0)
        except Exception:  # pragma: no cover
            return 0

    def count(self) -> int:
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()
                return int(row[0]) if row else 0
        except Exception:  # pragma: no cover
            return 0


class Cache:
    """统一缓存入口，屏蔽后端差异；Redis 异常时自动降级。

    探测方式很关键：必须用 `ping()` 判断，**不能用 set/get**——
    因为 set/get 都会吞异常（为了不让缓存故障影响主流程），
    用它们探测会把"连不上"误判成"连上了"，于是永远缓存不命中，
    遇到会挂住的网络还会把请求拖死。踩过这个坑，所以这里显式 ping。
    """

    def __init__(self) -> None:
        settings = get_settings()
        backend = settings.cache_backend
        self._backend: CacheBackend
        self.degraded_reason: str | None = None
        self.runtime_error: str | None = None
        self._fallback_name = "memory"

        if backend == "memory":
            self._backend = MemoryCache()
        else:
            try:
                candidate = RedisCache(settings.redis_url)
                if not candidate.ping():
                    raise ConnectionError("Redis ping 未返回成功")
                self._backend = candidate
                logger.info("缓存后端：Redis（%s）", settings.redis_url)
            except Exception as exc:
                self._backend, self._fallback_name = self._build_fallback(settings)
                self.degraded_reason = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "Redis 不可用，缓存降级为 %s（多副本不再共享同一份 Redis，"
                    "限流计数也只在本进程/本库生效）: %s",
                    "落盘 SQLite" if self._fallback_name == "sqlite" else "进程内内存",
                    exc,
                )
        self._ttl = settings.cache_ttl_seconds

    @staticmethod
    def _build_fallback(settings) -> tuple[CacheBackend, str]:
        """降级后端：默认落盘（重启后仍命中），显式配 memory 才用纯内存。"""
        if settings.cache_fallback == "memory":
            return MemoryCache(), "memory"
        try:
            cache = SqliteCache(settings.cache_sqlite_path)
            pruned = cache.prune_expired()
            if pruned:
                logger.info("降级缓存清理了 %s 条过期记录", pruned)
            return cache, "sqlite"
        except Exception as exc:  # pragma: no cover - 磁盘不可写时退回内存
            logger.warning("落盘缓存不可用，退回进程内缓存: %s", exc)
            return MemoryCache(), "memory"

    @property
    def backend_name(self) -> str:
        if isinstance(self._backend, RedisCache):
            return "redis"
        return self._fallback_name  # memory / sqlite

    @property
    def healthy(self) -> bool:
        """当前后端是否可用（降级时 false，供 /health 如实报告）。"""
        return self.degraded_reason is None and self.runtime_error is None

    def describe(self) -> dict:
        payload = {
            "backend": self.backend_name,
            "configured": get_settings().cache_backend,
            "healthy": self.healthy,
            "degraded_reason": self.degraded_reason,
            "runtime_error": self.runtime_error,
        }
        if isinstance(self._backend, SqliteCache):
            # 落盘缓存：说明它跨重启存活，并给出当前条目数（便于判断是不是"白配了"）。
            # 这里只给**文件名**不给完整路径：`/health` 是公开端点，没必要把部署目录暴露出去
            # （完整路径在 preflight_check.py 的输出里给运维看）。
            payload["persistent"] = True
            payload["entries"] = self._backend.count()
            payload["path"] = Path(self._backend._path).name
        elif isinstance(self._backend, MemoryCache):
            payload["persistent"] = False
        return payload

    def get(self, key: str) -> Any | None:
        return self._backend.get(key)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._backend.set(key, value, ttl or self._ttl)

    def delete(self, key: str) -> None:
        self._backend.delete(key)

    def increment(self, key: str, *, ttl: int) -> int:
        try:
            value = self._backend.increment(key, ttl=ttl)
        except Exception as exc:
            if isinstance(self._backend, RedisCache):
                # 只公开异常类型，不把可能含凭据的连接串/正文放进健康接口。
                self.runtime_error = type(exc).__name__
            raise
        if isinstance(self._backend, RedisCache):
            self.runtime_error = None
        return value


_cache: Cache | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache()
    return _cache

"""固定窗口限流必须在并发下保持上限。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.cache import Cache, MemoryCache, RedisCache, SqliteCache
from app.services.limits import RateLimiter, RateLimitUnavailable


def test_内存限流并发递增不丢计数():
    limiter = RateLimiter()
    limiter.cache = MemoryCache()

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(
            pool.map(
                lambda _index: limiter.check("concurrent-login", limit=20),
                range(100),
            )
        )

    assert sum(1 for allowed, _ in results if allowed) == 20
    assert sum(1 for allowed, _ in results if not allowed) == 80


def test_sqlite_限流跨线程原子递增(tmp_path):
    cache = SqliteCache(str(tmp_path / "rate-limit.db"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _index: cache.increment("counter", ttl=60), range(50)))

    assert sorted(values) == list(range(1, 51))
    assert cache.get("counter") == 50


def test_security_limit_fails_closed_and_recovers_without_leaking_errors(caplog):
    class RecoverableCache:
        backend_name = "redis"
        broken = True

        def increment(self, key, *, ttl):
            if self.broken:
                raise ConnectionError("redis://secret-password@internal")
            return 1

    limiter = RateLimiter()
    limiter.cache = RecoverableCache()
    with pytest.raises(RateLimitUnavailable):
        limiter.check("login:hash", limit=10, fail_closed=True, require_shared=True)
    assert "secret-password" not in caplog.text
    assert limiter.check("task", limit=10) == (True, 0)
    limiter.cache.broken = False
    assert limiter.check("login:hash", limit=10, fail_closed=True, require_shared=True) == (True, 0)


def test_redis_increment_failure_updates_health_and_success_recovers(monkeypatch):
    cache = Cache.__new__(Cache)
    cache._backend = RedisCache.__new__(RedisCache)
    cache.degraded_reason = None
    cache.runtime_error = None
    cache._fallback_name = "memory"
    cache._ttl = 300

    monkeypatch.setattr(
        cache._backend,
        "increment",
        lambda key, ttl: (_ for _ in ()).throw(ConnectionError("redis://secret@internal")),
    )
    with pytest.raises(ConnectionError):
        cache.increment("rate", ttl=60)
    assert cache.healthy is False
    assert cache.describe()["runtime_error"] == "ConnectionError"
    assert "secret" not in str(cache.describe())

    monkeypatch.setattr(cache._backend, "increment", lambda key, ttl: 1)
    assert cache.increment("rate", ttl=60) == 1
    assert cache.healthy is True
    assert cache.describe()["runtime_error"] is None

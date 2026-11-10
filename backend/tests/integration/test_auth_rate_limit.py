"""登录防暴力尝试：统一错误、摘要键、直连 IP 与成功重置。"""

from __future__ import annotations

import re

import pytest


class FakeRateLimiter:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.seen_keys: list[str] = []

    def check(self, key: str, *, limit: int, window_seconds: int = 60,
              fail_closed: bool = False, require_shared: bool = False) -> tuple[bool, int]:
        _ = window_seconds
        self.seen_keys.append(key)
        current = self.counts.get(key, 0)
        if limit > 0 and current >= limit:
            return False, 37
        self.counts[key] = current + 1
        return True, 0

    def reset(self, key: str, *, window_seconds: int = 60) -> None:
        _ = window_seconds
        self.counts.pop(key, None)


@pytest.fixture()
def login_limiter(monkeypatch):
    from app.api import routes_auth

    limiter = FakeRateLimiter()
    settings = routes_auth.get_settings().model_copy(
        update={"login_attempt_rate_limit_per_minute": 2}
    )
    monkeypatch.setattr(routes_auth, "get_rate_limiter", lambda: limiter)
    monkeypatch.setattr(routes_auth, "get_settings", lambda: settings)
    return limiter


def test_连续失败后返回_429_且不信任转发头(client, login_limiter):
    payload = {"username": "unknown-account", "password": "wrong-password"}
    first = client.post(
        "/api/v1/auth/login",
        json=payload,
        headers={"X-Forwarded-For": "198.51.100.1"},
    )
    second = client.post(
        "/api/v1/auth/login",
        json=payload,
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    blocked = client.post(
        "/api/v1/auth/login",
        json=payload,
        headers={"X-Forwarded-For": "192.0.2.44"},
    )

    assert first.status_code == second.status_code == 401
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "login_rate_limited"
    assert blocked.headers["Retry-After"] == "37"
    assert len(set(login_limiter.seen_keys)) == 1


def test_未知与已知账号失败时使用统一错误(client, login_limiter):
    unknown = client.post(
        "/api/v1/auth/login",
        json={"username": "missing-user", "password": "wrong-password"},
    )
    known = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong-password"},
    )

    assert unknown.status_code == known.status_code == 401
    assert unknown.json() == known.json()
    assert unknown.json()["detail"]["code"] == "invalid_credentials"


def test_失败账号不影响其他账号且成功会重置窗口(client, login_limiter):
    for _ in range(2):
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "blocked-user", "password": "wrong-password"},
        )
        assert response.status_code == 401

    success = client.post(
        "/api/v1/auth/login",
        json={"username": " PM ", "password": "pm123"},
    )
    assert success.status_code == 200

    # 同一账号先失败一次、再成功，成功后应开启新窗口而不是继承旧计数。
    failed = client.post(
        "/api/v1/auth/login",
        json={"username": "ops", "password": "wrong-password"},
    )
    assert failed.status_code == 401
    assert client.post(
        "/api/v1/auth/login", json={"username": "ops", "password": "ops123"}
    ).status_code == 200
    assert client.post(
        "/api/v1/auth/login", json={"username": "ops", "password": "wrong-password"}
    ).status_code == 401


def test_限流缓存键不含明文账号或口令(client, login_limiter):
    client.post(
        "/api/v1/auth/login",
        json={"username": "SensitiveUser", "password": "super-secret-password"},
    )

    assert login_limiter.seen_keys
    assert all(re.fullmatch(r"login:[0-9a-f]{24}", key) for key in login_limiter.seen_keys)
    joined = " ".join(login_limiter.seen_keys).lower()
    assert "sensitiveuser" not in joined
    assert "super-secret-password" not in joined


@pytest.mark.parametrize("failure", ["outage", "fallback"])
def test_login_backend_failure_blocks_before_password_check(client, monkeypatch, failure):
    from app.api import routes_auth
    from app.services.cache import MemoryCache
    from app.services.limits import RateLimiter

    limiter = RateLimiter()
    if failure == "outage":
        class BrokenCache:
            backend_name = "redis"

            def increment(self, key, *, ttl):
                raise ConnectionError("redis://private-credential@internal")

        limiter.cache = BrokenCache()
    else:
        class FallbackCache(MemoryCache):
            backend_name = "memory"

        limiter.cache = FallbackCache()
    settings = routes_auth.get_settings().model_copy(update={"app_env": "prod"})
    monkeypatch.setattr(routes_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(routes_auth, "get_rate_limiter", lambda: limiter)

    def unexpected_password_check(*args):
        pytest.fail("限流故障时不应继续验证密码或签发令牌")

    monkeypatch.setattr(routes_auth, "verify_password", unexpected_password_check)
    response = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert response.json()["detail"]["code"] == "login_temporarily_unavailable"
    assert "private-credential" not in response.text
    assert "access_token" not in response.text

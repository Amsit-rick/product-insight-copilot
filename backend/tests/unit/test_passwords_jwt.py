"""口令哈希（`app.auth.passwords`）与 JWT（`app.auth.jwt`）单元测试。"""

from __future__ import annotations

import pytest

from app.auth.jwt import TokenError, create_access_token, decode_access_token
from app.auth.passwords import hash_password, verify_password
from app.config import get_settings


# --------------------------------------------------------------------------- 口令
def test_哈希与校验往返成功():
    hashed = hash_password("Str0ng-Passw0rd!")

    assert hashed != "Str0ng-Passw0rd!"
    assert hashed.startswith("pbkdf2_sha256$")
    assert verify_password("Str0ng-Passw0rd!", hashed) is True


def test_同一口令两次哈希不相同_因为盐随机():
    first = hash_password("same-password")
    second = hash_password("same-password")

    assert first != second
    assert verify_password("same-password", first) is True
    assert verify_password("same-password", second) is True


def test_错误口令校验失败():
    hashed = hash_password("correct-horse")

    assert verify_password("wrong-horse", hashed) is False
    assert verify_password("", hashed) is False
    assert verify_password("correct-horse ", hashed) is False


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "plain-text-not-a-hash",
        "pbkdf2_sha256$not-an-int$c2FsdA==$aGFzaA==",
        "pbkdf2_sha256$120000$not-base64!$aGFzaA==",
        "pbkdf2_sha256$120000$c2FsdA==",  # 段数不足
        "$argon2id$v=19$m=65536,t=3,p=4$broken",
        "md5$deadbeef",
    ],
)
def test_畸形哈希一律返回False而不抛异常(malformed: str):
    assert verify_password("whatever", malformed) is False


def test_哈希片段完整且迭代次数固定():
    hashed = hash_password("pw")
    algorithm, iterations, salt_b64, digest_b64 = hashed.split("$")

    assert algorithm == "pbkdf2_sha256"
    assert int(iterations) == 120_000
    assert salt_b64 and digest_b64


def test_配置切换为argon2时写入argon2哈希(monkeypatch):
    """password_scheme 为 argon2 时使用 argon2（本环境已安装 argon2-cffi）。"""
    import app.auth.passwords as passwords

    patch = get_settings().model_copy(update={"password_scheme": "argon2"})
    monkeypatch.setattr(passwords, "get_settings", lambda: patch)

    hashed = hash_password("argon2-pw")

    assert hashed.startswith("$argon2")
    assert verify_password("argon2-pw", hashed) is True
    assert verify_password("bad-pw", hashed) is False


# --------------------------------------------------------------------------- JWT
def test_签发的令牌能解回全部声明():
    token = create_access_token(
        subject="user-1", tenant_id="tenant-a", roles=["admin", "reviewer"], security_level=2
    )
    payload = decode_access_token(token)

    assert payload["sub"] == "user-1"
    assert payload["tenant_id"] == "tenant-a"
    assert payload["roles"] == ["admin", "reviewer"]
    assert payload["security_level"] == 2
    assert payload["iss"] == "product-insight-copilot"
    assert payload["exp"] > payload["iat"]


def test_默认有效期取配置的12小时():
    from datetime import datetime, timezone

    token = create_access_token("u", "t", [])
    payload = decode_access_token(token)

    expires_minutes = get_settings().jwt_expire_minutes
    assert expires_minutes == 60 * 12
    lifetime = datetime.fromtimestamp(payload["exp"], timezone.utc) - datetime.fromtimestamp(
        payload["iat"], timezone.utc
    )
    assert abs(lifetime.total_seconds() - expires_minutes * 60) < 5


def test_自定义有效期():
    token = create_access_token("u", "t", [], expires_minutes=1)
    payload = decode_access_token(token)

    assert payload["exp"] - payload["iat"] <= 61


def test_已过期令牌抛出TokenError():
    token = create_access_token("u", "t", [], expires_minutes=-1)

    with pytest.raises(TokenError, match="expired"):
        decode_access_token(token)


def test_被篡改的令牌抛出TokenError():
    token = create_access_token("user-1", "tenant-a", ["admin"])
    head, body, signature = token.split(".")
    tampered = f"{head}.{body}.{'A' * len(signature)}"

    with pytest.raises(TokenError):
        decode_access_token(tampered)


def test_篡改载荷后签名失效():
    import base64
    import json

    token = create_access_token("user-1", "tenant-a", ["viewer"])
    head, body, signature = token.split(".")
    padding = "=" * (-len(body) % 4)
    claims = json.loads(base64.urlsafe_b64decode(body + padding))
    claims["roles"] = ["admin"]
    forged_body = (
        base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
    )

    with pytest.raises(TokenError):
        decode_access_token(f"{head}.{forged_body}.{signature}")


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "not.a.jwt", "e30.e30.e30"])
def test_垃圾令牌抛出TokenError(garbage: str):
    with pytest.raises(TokenError):
        decode_access_token(garbage)


def test_签发者不符时抛出TokenError():
    """iss 固定为 product-insight-copilot，换签发者的令牌必须被拒。"""
    import jwt as pyjwt

    settings = get_settings()
    forged = pyjwt.encode(
        {"sub": "u", "iss": "someone-else"}, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )

    with pytest.raises(TokenError, match="issuer"):
        decode_access_token(forged)


def test_用其它密钥签发的令牌被拒绝():
    import jwt as pyjwt

    forged = pyjwt.encode(
        {"sub": "u", "iss": "product-insight-copilot"},
        "other-test-secret-at-least-32-bytes-long",
        algorithm="HS256",
    )

    with pytest.raises(TokenError):
        decode_access_token(forged)

"""口令哈希。

默认 PBKDF2-HMAC-SHA256（标准库实现，零依赖风险）；
可通过配置切换到 argon2（需要 argon2-cffi）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from app.config import get_settings

_PBKDF2_ITERATIONS = 120_000


def _pbkdf2_hash(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def _pbkdf2_verify(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, hash_b64 = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def hash_password(password: str) -> str:
    settings = get_settings()
    if settings.password_scheme == "argon2":
        try:
            from argon2 import PasswordHasher

            return PasswordHasher().hash(password)
        except Exception:
            # argon2 不可用时回退到 PBKDF2，保证服务可用
            pass
    return _pbkdf2_hash(password, secrets.token_bytes(16))


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    if hashed.startswith("pbkdf2_sha256$"):
        return _pbkdf2_verify(password, hashed)
    if hashed.startswith("$argon2"):
        try:
            from argon2 import PasswordHasher
            from argon2.exceptions import VerifyMismatchError

            ph = PasswordHasher()
            ph.verify(hashed, password)
            return True
        except VerifyMismatchError:
            return False
        except Exception:
            return False
    return False

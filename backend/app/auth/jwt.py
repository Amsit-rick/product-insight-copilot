"""JWT 签发与校验。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from jwt import InvalidTokenError

from app.config import get_settings


class TokenError(Exception):
    pass


def create_access_token(
    subject: str,
    tenant_id: str,
    roles: list[str],
    security_level: int = 1,
    expires_minutes: int | None = None,
) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.jwt_expire_minutes
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "tenant_id": tenant_id,
        "roles": roles,
        "security_level": security_level,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "iss": "product-insight-copilot",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer="product-insight-copilot",
        )
    except InvalidTokenError as exc:
        raise TokenError(str(exc)) from exc

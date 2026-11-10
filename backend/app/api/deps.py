"""API 依赖：认证、权限、分页与租户作用域。

安全约定（设计文档 11 节）：
- 身份与权限**只从服务端会话/令牌解析**，不信任请求体里的 tenant_id / roles；
- 每个请求一个 Session，事务边界交给依赖收尾；
- 越权统一返回 403，资源不存在返回 404，不泄露其他租户的资源是否存在。
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from hmac import compare_digest

from fastapi import Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.auth.jwt import TokenError, decode_access_token
from app.auth.rbac import allowed_actions, allowed_knowledge_bases, can_access
from app.config import get_settings
from app.db.session import get_db
from app.models.identity import User


@dataclass
class Principal:
    """当前请求主体：用户 + 计算出的权限集合。"""

    user: User
    actions: set[str]
    knowledge_bases: set[str]

    @property
    def tenant_id(self) -> str:
        return self.user.tenant_id

    @property
    def roles(self) -> list[str]:
        return self.user.role_codes

    def require(self, action: str) -> None:
        if action not in self.actions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "permission_denied", "message": f"缺少权限: {action}"},
            )

    def can(self, action: str) -> bool:
        return action in self.actions


def get_current_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_db),
) -> Principal:
    settings = get_settings()
    auth_source = "bearer"
    token = ""
    if authorization:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "invalid_authorization", "message": "Authorization 令牌格式无效"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = authorization.split(" ", 1)[1].strip()
    else:
        auth_source = "cookie"
        token = request.cookies.get(settings.auth_cookie_name, "")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthenticated", "message": "缺少登录凭据"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_access_token(token)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = session.get(User, payload.get("sub"))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "user_inactive", "message": "用户不存在或已停用"},
        )
    # Cookie 会话需要双提交 CSRF 令牌；Bearer 兼容调用由 Authorization 头本身承担
    # 请求来源证明，因此保持现有脚本/服务的兼容性。
    if auth_source == "cookie" and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        csrf_cookie = request.cookies.get(settings.auth_csrf_cookie_name, "")
        csrf_header = request.headers.get(settings.auth_csrf_header_name, "")
        if not csrf_cookie or not csrf_header or not compare_digest(csrf_cookie, csrf_header):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "csrf_failed", "message": "缺少或无效的 CSRF 令牌"},
            )
    roles = user.role_codes
    return Principal(
        user=user,
        actions=allowed_actions(roles),
        knowledge_bases=allowed_knowledge_bases(roles),
    )


def require_action(action: str):
    """依赖工厂：在路由上声明所需动作权限。"""

    def dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        principal.require(action)
        return principal

    return dependency


@dataclass
class Pagination:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def get_pagination(
    page: int = Query(default=1, ge=1, le=1000),
    page_size: int = Query(default=20, ge=1, le=200),
) -> Pagination:
    return Pagination(page=page, page_size=page_size)


def get_session() -> Generator[Session, None, None]:
    yield from get_db()


__all__ = [
    "Pagination",
    "Principal",
    "get_current_principal",
    "get_pagination",
    "get_session",
    "require_action",
    "can_access",
]

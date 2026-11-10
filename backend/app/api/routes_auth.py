"""认证接口：登录、当前用户、账号列表。"""

from __future__ import annotations

from hashlib import sha256
from secrets import token_urlsafe

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_current_principal
from app.auth.jwt import create_access_token
from app.auth.passwords import verify_password
from app.config import get_settings
from app.db.session import get_db
from app.models.identity import Tenant, User
from app.schemas.auth import LoginRequest, MeOut, SessionResponse, TokenResponse, UserOut
from app.services.audit import record_audit
from app.services.limits import RateLimitUnavailable, get_rate_limiter

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/login", response_model=TokenResponse, summary="登录并获取访问令牌")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_db),
) -> TokenResponse:
    settings = get_settings()
    token, user = _authenticate(payload, request, session, settings)
    _set_no_store_headers(response)
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
        user=_user_out(user),
    )


@router.post("/session", response_model=SessionResponse, summary="建立浏览器安全会话")
def create_session(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_db),
) -> SessionResponse:
    settings = get_settings()
    token, user = _authenticate(payload, request, session, settings)
    _set_auth_cookies(response, token, settings)
    _set_no_store_headers(response)
    return SessionResponse(
        expires_in=settings.jwt_expire_minutes * 60,
        user=_user_out(user),
        csrf_cookie_name=settings.auth_csrf_cookie_name,
        csrf_header_name=settings.auth_csrf_header_name,
    )


def _authenticate(payload: LoginRequest, request: Request, session: Session, settings) -> tuple[str, User]:
    """验证账号并签发一次新 JWT；由调用端决定放入 JSON 还是 HttpOnly Cookie。"""

    username = payload.username.strip().lower()
    rate_key = _login_rate_limit_key(request, username)
    limiter = get_rate_limiter()
    try:
        allowed, retry_after = limiter.check(
            rate_key,
            limit=settings.login_attempt_rate_limit_per_minute,
            window_seconds=60,
            fail_closed=True,
            require_shared=settings.app_env == "prod",
        )
    except RateLimitUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "login_temporarily_unavailable", "message": "登录服务暂不可用，请稍后重试"},
            headers={"Retry-After": "30"},
        ) from None
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "login_rate_limited",
                "message": f"登录尝试过于频繁，请 {retry_after} 秒后重试",
            },
            headers={"Retry-After": str(retry_after)},
        )

    user = session.query(User).filter(User.username == username).first()
    # 统一错误信息，避免暴露"用户是否存在"
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "invalid_credentials", "message": "用户名或密码错误"},
    )
    if user is None or not user.is_active:
        raise invalid
    if not verify_password(payload.password, user.hashed_password):
        record_audit(
            session,
            action="login_failed",
            tenant_id=user.tenant_id,
            actor_id=user.id,
            actor_name=user.username,
            outcome="denied",
        )
        session.commit()
        raise invalid

    if payload.tenant_code:
        tenant = session.get(Tenant, user.tenant_id)
        if tenant is None or tenant.code != payload.tenant_code:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "tenant_mismatch", "message": "账号不属于该租户"},
            )

    token = create_access_token(
        subject=user.id,
        tenant_id=user.tenant_id,
        roles=user.role_codes,
        security_level=user.security_level,
    )
    from app.models.base import utcnow

    user.last_login_at = utcnow()
    record_audit(
        session,
        action="login_success",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        actor_name=user.username,
        detail={"roles": user.role_codes},
    )
    session.commit()
    limiter.reset(rate_key, window_seconds=60)
    return token, user


def _login_rate_limit_key(request: Request, normalized_username: str) -> str:
    """构造不可逆缓存键；只信任直连地址，不采信可由客户端伪造的转发头。"""

    client_ip = request.client.host if request.client else "unknown"
    digest = sha256(f"{client_ip}\0{normalized_username}".encode("utf-8")).hexdigest()[:24]
    return f"login:{digest}"


def _set_auth_cookies(response: Response, token: str, settings) -> None:
    max_age = settings.jwt_expire_minutes * 60
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=max_age,
        expires=max_age,
        path="/",
        secure=settings.auth_cookie_secure_effective,
        httponly=True,
        samesite=settings.auth_cookie_samesite,
    )
    # 双提交模式：CSRF Cookie 需要由同源前端读取并回传 Header，因此不能 HttpOnly。
    response.set_cookie(
        key=settings.auth_csrf_cookie_name,
        value=token_urlsafe(32),
        max_age=max_age,
        expires=max_age,
        path="/",
        secure=settings.auth_cookie_secure_effective,
        httponly=False,
        samesite=settings.auth_cookie_samesite,
    )


def _set_no_store_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="退出当前会话")
def logout(response: Response) -> None:
    """清理浏览器会话；即使会话已过期也返回成功，便于前端幂等退出。"""

    settings = get_settings()
    for name in (settings.auth_cookie_name, settings.auth_csrf_cookie_name):
        response.delete_cookie(
            key=name,
            path="/",
            secure=settings.auth_cookie_secure_effective,
            httponly=name == settings.auth_cookie_name,
            samesite=settings.auth_cookie_samesite,
        )


@router.get("/me", response_model=MeOut, summary="当前登录用户与权限")
def me(principal: Principal = Depends(get_current_principal)) -> MeOut:
    user = principal.user
    settings = get_settings()
    return MeOut(
        **_user_out(user).model_dump(),
        actions=sorted(principal.actions),
        knowledge_bases=sorted(principal.knowledge_bases),
        csrf_cookie_name=settings.auth_csrf_cookie_name,
        csrf_header_name=settings.auth_csrf_header_name,
    )


@router.get("/users", response_model=list[UserOut], summary="账号列表（仅管理员）")
def list_users(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> list[UserOut]:
    principal.require("admin:manage")
    users = (
        session.query(User)
        .filter(User.tenant_id == principal.tenant_id)
        .order_by(User.username)
        .all()
    )
    return [_user_out(user) for user in users]


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        email=user.email,
        department=user.department,
        tenant_id=user.tenant_id,
        security_level=user.security_level,
        roles=user.role_codes,
    )

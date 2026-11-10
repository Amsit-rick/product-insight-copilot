"""认证与身份契约。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=4, max_length=128)
    tenant_code: str | None = Field(default=None, max_length=64)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    full_name: str | None = None
    email: str | None = None
    department: str | None = None
    tenant_id: str
    security_level: int
    roles: list[str] = Field(default_factory=list)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class SessionResponse(BaseModel):
    """浏览器会话响应：身份令牌仅写入 HttpOnly Cookie，不进入 JSON。"""

    expires_in: int
    user: UserOut
    # 前端不能读取服务端环境变量；返回非敏感的 CSRF 参数，避免部署改名后写请求全部 403。
    csrf_cookie_name: str
    csrf_header_name: str


class MeOut(UserOut):
    """当前登录用户：附带权限计算结果，前端据此隐藏入口。"""

    actions: list[str] = Field(default_factory=list)
    knowledge_bases: list[str] = Field(default_factory=list)
    csrf_cookie_name: str
    csrf_header_name: str

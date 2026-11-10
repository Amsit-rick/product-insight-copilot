"""指标密级策略的请求/响应契约。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AccessPolicyCreate(BaseModel):
    subject_type: str = Field(default="role", description="role | user")
    subject_id: str = Field(min_length=1, max_length=64, description="角色码或用户 id；* 表示所有主体")
    resource_type: str = Field(default="metric", description="metric | metric_domain | dataset")
    resource_id: str = Field(min_length=1, max_length=64, description="资源名，支持 * 与 前缀*")
    effect: str = Field(default="allow", description="allow | deny")
    actions: list[str] = Field(default_factory=lambda: ["metric:read"])
    # 细粒度条件：min_clearance / valid_from / valid_until / time_window / row_filters / timezone
    conditions: dict = Field(default_factory=dict)


class PolicySimulateRequest(BaseModel):
    """权限预演：用"某个账号/某组角色"去问"这个指标能不能读、为什么"。

    主体二选一：给 `username` 就按该账号真实的角色与密级判定；
    或者直接给 `roles` + `clearance`，用于验证"改成这个角色之后会怎样"。
    """

    username: str | None = Field(default=None, max_length=64, description="按某个账号的真实角色与密级预演")
    roles: list[str] = Field(default_factory=list, description="直接指定角色码（与 username 二选一）")
    clearance: int | None = Field(default=None, ge=0, le=9, description="直接指定密级（配合 roles 使用）")
    metric: str = Field(min_length=1, max_length=64, description="要预演的指标/资源名")
    resource_type: str = Field(default="metric", description="metric | metric_domain | dataset")
    # 想顺便验证"某个具体维度取值是否在范围内"（如 channel=channel_b）
    requested: dict[str, str] = Field(default_factory=dict, description="待验证的维度取值，如 {channel: channel_b}")

    @field_validator("metric")
    @classmethod
    def _strip_metric(cls, value: str) -> str:
        stripped = (value or "").strip()
        if not stripped:
            raise ValueError("指标名不能为空")
        return stripped

    @model_validator(mode="after")
    def _require_subject(self) -> "PolicySimulateRequest":
        if not (self.username or "").strip() and not self.roles:
            raise ValueError("请提供 username，或直接给出 roles")
        return self


class AuthorizationSimulateRequest(BaseModel):
    """统一 RBAC + ABAC 预演输入，与真实业务授权使用同一决策服务。"""

    username: str | None = Field(default=None, max_length=64)
    roles: list[str] = Field(default_factory=list)
    clearance: int | None = Field(default=None, ge=0, le=9)
    department: str | None = Field(default=None, max_length=64)
    action: str = Field(min_length=1, max_length=64)
    resource_type: str = Field(min_length=1, max_length=32)
    resource_id: str = Field(min_length=1, max_length=128)
    resource_tenant_id: str | None = Field(default=None, max_length=36)
    context: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_subject(self) -> "AuthorizationSimulateRequest":
        if not (self.username or "").strip() and not self.roles:
            raise ValueError("请提供 username，或直接给出 roles")
        return self


class AccessPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    subject_type: str
    subject_id: str
    resource_type: str
    resource_id: str
    effect: str
    actions: list[str]
    conditions: dict
    created_at: datetime | None = None

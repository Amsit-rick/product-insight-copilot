"""身份、租户、角色与访问策略。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.models.base import Base, TimestampMixin, UUIDPkMixin
from app.models.enums import RoleCode


class Tenant(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="active")


class Role(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)


class User(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "users"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id"), index=True, nullable=False
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(128))
    full_name: Mapped[str | None] = mapped_column(String(64))
    hashed_password: Mapped[str] = mapped_column(String(255))
    department: Mapped[str | None] = mapped_column(String(64))
    # 用户可访问的最高密级（SecurityLevel.rank）
    security_level: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    roles: Mapped[list["UserRole"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def role_codes(self) -> list[str]:
        return [ur.role_code for ur in self.roles]

    def has_role(self, *codes: str | RoleCode) -> bool:
        wanted = {c.value if isinstance(c, RoleCode) else c for c in codes}
        return bool(wanted & set(self.role_codes))


class UserRole(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_code", name="uq_user_role"),)

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    role_code: Mapped[str] = mapped_column(String(64), index=True)

    user: Mapped[User] = relationship(back_populates="roles", lazy="joined")


class AccessPolicy(UUIDPkMixin, TimestampMixin, Base):
    """细粒度访问策略：主体（角色/用户）对资源（知识库/文档/指标）的动作许可。"""

    __tablename__ = "access_policies"

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    subject_type: Mapped[str] = mapped_column(String(16), default="role")  # role | user
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(32), default="knowledge_base")
    resource_id: Mapped[str] = mapped_column(String(64), default="*", index=True)
    effect: Mapped[str] = mapped_column(String(8), default="allow")  # allow | deny
    actions: Mapped[list] = mapped_column(JSON, default=list)
    conditions: Mapped[dict] = mapped_column(JSON, default=dict)

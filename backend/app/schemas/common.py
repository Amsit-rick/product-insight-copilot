"""通用响应结构。"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class PageMeta(BaseModel):
    total: int = 0
    page: int = 1
    page_size: int = 20


class ListResponse(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    meta: PageMeta = Field(default_factory=PageMeta)


class ErrorResponse(BaseModel):
    """统一错误结构：code 供前端分支，message 面向用户，detail 仅用于排查。"""

    model_config = ConfigDict(extra="allow")

    code: str
    message: str
    detail: dict | None = None
    request_id: str | None = None

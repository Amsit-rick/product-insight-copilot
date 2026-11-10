"""知识库问答的请求契约。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator


class QaAskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000, description="要问的问题")
    conversation_id: str | None = Field(
        default=None,
        min_length=36,
        max_length=36,
        description="继续已有会话；为空时首次成功提问自动创建会话",
    )
    knowledge_bases: list[str] = Field(
        default_factory=list,
        description="限定知识库；为空表示在当前用户可见范围内检索。显式传入无权库会被 403 拒绝",
    )
    app_version: str | None = Field(default=None, max_length=32)
    date_from: date | None = None
    date_to: date | None = None
    top_k: int = Field(default=6, ge=1, le=20)
    # 是否允许用模型归纳答案；关闭时纯检索式回答（离线可跑、秒级返回）
    use_model: bool = True

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        """空白问题按"没有提问"处理（422），而不是走权限/检索逻辑。"""
        stripped = (value or "").strip()
        if not stripped:
            raise ValueError("问题不能为空")
        return stripped

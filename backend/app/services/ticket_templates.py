"""客服常用回复模板的业务规则。

模板只负责生成草稿，发送仍必须经过工单回复/审批门禁。变量替换保持确定性，
避免把订单事实或金额交给模型二次改写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BuiltinReplyTemplate:
    title: str
    content: str
    issue_type: str | None = None


BUILTIN_REPLY_TEMPLATES: tuple[BuiltinReplyTemplate, ...] = (
    BuiltinReplyTemplate(
        title="等待补充信息",
        content="您好，为了尽快帮您处理{{问题类型}}，请补充页面提示的相关信息。收到后我们会继续为您核实。",
    ),
    BuiltinReplyTemplate(
        title="已提交主管审核",
        content="您好，订单{{订单号}}的处理方案已提交主管审核，审核结果会第一时间同步给您。",
    ),
    BuiltinReplyTemplate(
        title="物流核实中",
        content="您好，订单{{订单号}}的物流情况正在核实中，确认后我们会及时向您同步处理结果。",
        issue_type="logistics_stalled",
    ),
)


_VARIABLES = re.compile(r"\{\{([^{}]+)\}\}")


def apply_reply_template(content: str, values: dict[str, object | None]) -> tuple[str, list[str]]:
    """替换模板变量并返回仍未替换的变量名。"""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        value = values.get(name)
        return str(value) if value is not None and str(value).strip() else match.group(0)

    rendered = _VARIABLES.sub(replace, content)
    missing = list(dict.fromkeys(match.group(1).strip() for match in _VARIABLES.finditer(rendered)))
    return rendered, missing


__all__ = ["BUILTIN_REPLY_TEMPLATES", "BuiltinReplyTemplate", "apply_reply_template"]

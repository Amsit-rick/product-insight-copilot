"""角色与知识库访问策略（设计文档 4.1 / 9.3 节）。

权限过滤是普通代码职责，不交给 Agent 判断。
"""

from __future__ import annotations

from app.models.enums import KnowledgeBaseCode, RoleCode

# 角色 -> 允许访问的知识库
ROLE_KB_ACCESS: dict[str, set[str]] = {
    RoleCode.ADMIN.value: {kb.value for kb in KnowledgeBaseCode},
    RoleCode.PRODUCT_MANAGER.value: {
        KnowledgeBaseCode.PRODUCT.value,
        KnowledgeBaseCode.FEEDBACK.value,
        KnowledgeBaseCode.OPERATION.value,
        KnowledgeBaseCode.ENGINEERING.value,
        # 政策库是全员都要读的规则来源（客服/主管/质检都要引政策依据）
        KnowledgeBaseCode.POLICY.value,
    },
    RoleCode.OPERATION.value: {
        KnowledgeBaseCode.FEEDBACK.value,
        KnowledgeBaseCode.OPERATION.value,
        KnowledgeBaseCode.POLICY.value,
    },
    RoleCode.ENGINEER.value: {
        KnowledgeBaseCode.ENGINEERING.value,
        KnowledgeBaseCode.OPERATION.value,
    },
    RoleCode.REVIEWER.value: {kb.value for kb in KnowledgeBaseCode},
}

# 角色 -> 允许的动作
#
# 售后工单的岗位映射（需求书 §1.3 定义了客服/售后主管/质检/管理员四个角色，
# 这里先复用既有角色，避免为 MVP 大改角色体系；映射关系写进文档，后续要独立角色再加）：
#   客服      -> operation（运营岗）
#   售后主管  -> reviewer（审核岗，带 ticket:approve）
#   质检      -> reviewer（只读 + 复核）
#   管理员    -> admin
ROLE_ACTIONS: dict[str, set[str]] = {
    RoleCode.ADMIN.value: {
        "task:create",
        "task:read",
        "task:cancel",
        "task:approve",
        "task:replay",
        "report:read",
        "document:upload",
        "document:delete",
        "kb:reindex",
        "metric:import",
        "metric:read",
        "eval:run",
        "admin:manage",
        "ticket:create",
        "ticket:read",
        "ticket:reply",
        "ticket:assign",
        "ticket:approve",
        "ticket:template_manage",
        "ticket:quality_review",
        # 政策库：管理员可写，其余角色按下面单独配置
        "policy:read",
        "policy:write",
    },
    RoleCode.PRODUCT_MANAGER.value: {
        "task:create",
        "task:read",
        "task:cancel",
        "report:read",
        "document:upload",
        "kb:reindex",
        "metric:import",
        "metric:read",
        "ticket:create",
        "ticket:read",
        "ticket:reply",
        "ticket:assign",
        "policy:read",
    },
    RoleCode.OPERATION.value: {
        "task:read",
        "report:read",
        "metric:import",
        "metric:read",
        # 客服岗：能建单、能改草稿并发送、能转派；政策只读（修订政策是主管/管理员的活）
        "ticket:create",
        "ticket:read",
        "ticket:reply",
        "ticket:assign",
        "policy:read",
    },
    RoleCode.ENGINEER.value: {
        "task:read",
        "report:read",
        "document:upload",
        "ticket:read",
        "metric:read",
    },
    RoleCode.REVIEWER.value: {
        "task:read",
        "report:read",
        "task:approve",
        "task:replay",
        "metric:read",
        # 售后主管/质检：能审批、能转派、能看全部，并且**能修订政策**（政策版本由主管维护）
        "ticket:read",
        "ticket:approve",
        "ticket:template_manage",
        "ticket:quality_review",
        "ticket:assign",
        "policy:read",
        "policy:write",
    },
}


def allowed_knowledge_bases(roles: list[str]) -> set[str]:
    result: set[str] = set()
    for role in roles:
        result |= ROLE_KB_ACCESS.get(role, set())
    return result


def allowed_actions(roles: list[str]) -> set[str]:
    result: set[str] = set()
    for role in roles:
        result |= ROLE_ACTIONS.get(role, set())
    return result


def can_access(roles: list[str], action: str) -> bool:
    return action in allowed_actions(roles)


class PermissionDenied(Exception):
    def __init__(self, action: str) -> None:
        super().__init__(f"缺少权限: {action}")
        self.action = action

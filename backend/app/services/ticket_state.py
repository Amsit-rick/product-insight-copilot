"""工单状态机（需求书 §2.3）。

为什么单独一个纯规则模块（不碰数据库）：

1. 需求书明确"**状态迁移必须由后端服务校验，前端不能直接改写状态**"。规则做成纯函数后
   可以单测每一条边，也便于前端用**同一份规则**渲染"当前允许哪些操作"——
   否则前后端各写一套，用户点到不允许的按钮才报错。
2. 状态机是业务口径的一部分：改一条边就是改口径，必须有一处能一眼看全的地方。

设计取舍：`RESOLVED` 与 `CANCELLED` 是**终态**（不提供 reopen）。客户再次追问按需求书
主线当作新工单处理——这样"已解决"的统计口径才干净；如果以后要支持 reopen，
只需在这里加 `RESOLVED -> ENRICHING` 一条边，并同步统计口径。
"""

from __future__ import annotations

from app.models.enums import TicketStatus

NEW = TicketStatus.NEW.value
ENRICHING = TicketStatus.ENRICHING.value
ANALYZING = TicketStatus.ANALYZING.value
WAITING_REVIEW = TicketStatus.WAITING_REVIEW.value
READY_TO_REPLY = TicketStatus.READY_TO_REPLY.value
RESOLVED = TicketStatus.RESOLVED.value
NEED_CUSTOMER_INFO = TicketStatus.NEED_CUSTOMER_INFO.value
ESCALATED = TicketStatus.ESCALATED.value
FAILED = TicketStatus.FAILED.value
CANCELLED = TicketStatus.CANCELLED.value

# 每个状态允许迁移到的状态集合（唯一事实来源）
TRANSITIONS: dict[str, set[str]] = {
    # 已受理：要么去补事实，要么直接发现信息不足，要么被取消/失败
    NEW: {ENRICHING, NEED_CUSTOMER_INFO, CANCELLED, FAILED},
    # 补事实：成功则进入分析；缺信息退回客户；接口异常则失败
    ENRICHING: {ANALYZING, NEED_CUSTOMER_INFO, FAILED, CANCELLED},
    # 分析完成：低风险可直接出草稿；高风险/低置信度去待审批；冲突可升级
    ANALYZING: {
        WAITING_REVIEW,
        READY_TO_REPLY,
        NEED_CUSTOMER_INFO,
        ESCALATED,
        FAILED,
        CANCELLED,
    },
    # 待审批：批准→待回复；驳回→重新分析；争议→升级
    WAITING_REVIEW: {READY_TO_REPLY, ANALYZING, ESCALATED, NEED_CUSTOMER_INFO, FAILED, CANCELLED},
    # 草稿就绪：客服发送→已解决；需要补充分析可回到分析；也能升级
    READY_TO_REPLY: {RESOLVED, ANALYZING, ESCALATED, WAITING_REVIEW, FAILED, CANCELLED},
    # 等客户补充：客户回了就重新补事实；也可以直接取消
    NEED_CUSTOMER_INFO: {ENRICHING, CANCELLED, FAILED},
    # 已升级（风控/主管）：处理后回到流程、直接解决或失败
    ESCALATED: {ENRICHING, READY_TO_REPLY, RESOLVED, FAILED, CANCELLED},
    # 失败可重试（回 ENRICHING 重跑事实与检索）
    FAILED: {ENRICHING, CANCELLED},
    # 终态
    RESOLVED: set(),
    CANCELLED: set(),
}

TERMINAL: set[str] = {RESOLVED, CANCELLED}


class InvalidTransition(Exception):
    """非法状态迁移。API 层统一映射为 409 + code=ticket_invalid_transition。"""

    code = "ticket_invalid_transition"

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"工单状态不允许从 {current} 迁移到 {target}")


def is_terminal(status: str) -> bool:
    return status in TERMINAL


def allowed_transitions(status: str) -> list[str]:
    return sorted(TRANSITIONS.get(status, set()))


def can_transition(current: str, target: str) -> bool:
    if current == target:
        return False
    return target in TRANSITIONS.get(current, set())


def assert_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)


def describe() -> dict:
    """给前端/文档用的状态机全貌（含终态标记），避免前端硬编码。"""
    return {
        status: {
            "next": sorted(targets),
            "terminal": status in TERMINAL,
        }
        for status, targets in TRANSITIONS.items()
    }

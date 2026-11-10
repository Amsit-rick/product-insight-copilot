"""HTTP 接口层：路由器聚合。

所有业务接口都挂在 `settings.api_prefix`（默认 `/api/v1`）之下；
`/health` 额外挂在根路径，供容器健康检查直接使用。
"""

from fastapi import APIRouter

from app.api.routes_access_policies import router as access_policies_router
from app.api.routes_approvals import router as approvals_router
from app.api.routes_auth import router as auth_router
from app.api.routes_documents import router as documents_router
from app.api.routes_evaluations import router as evaluations_router
from app.api.routes_metrics import router as metrics_router
from app.api.routes_operational_alerts import router as operational_alerts_router
from app.api.routes_policies import router as policies_router
from app.api.routes_qa import router as qa_router
from app.api.routes_queue import router as queue_router
from app.api.routes_reflection_release import router as reflection_release_router
from app.api.routes_reports import router as reports_router
from app.api.routes_system import router as system_router
from app.api.routes_tasks import router as tasks_router
from app.api.routes_tickets import router as tickets_router


def build_api_router() -> APIRouter:
    router = APIRouter()
    router.include_router(system_router)
    router.include_router(auth_router)
    # 售后工单（电商售后工单智能处理系统）：工作台、详情、建议、审批
    router.include_router(tickets_router)
    # 售后政策：版本、生效期、适用范围与检索测试
    router.include_router(policies_router)
    router.include_router(tasks_router)
    router.include_router(documents_router)
    router.include_router(metrics_router)
    router.include_router(reports_router)
    router.include_router(approvals_router)
    router.include_router(evaluations_router)
    router.include_router(access_policies_router)
    router.include_router(reflection_release_router)
    router.include_router(operational_alerts_router)
    router.include_router(qa_router)
    # 队列运维（死信排查与重放），仅管理员
    router.include_router(queue_router)
    return router


__all__ = ["build_api_router"]

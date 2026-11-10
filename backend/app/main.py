"""FastAPI 应用入口。

启动顺序（`lifespan`）：
1. 配置日志（密钥脱敏）；
2. 建表（幂等）；
3. 若库里还没有任何账号，则播种演示数据（可通过 AUTO_SEED=false 关闭）；
4. 关闭时优雅停止工作流线程池。

本地零依赖模式：SQLite + 进程内缓存 + 本地文件存储 + stub 模型，
一条命令即可跑通完整诊断链路；接入 Postgres/Redis/MinIO/DeepSeek 只需改环境变量。
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import build_api_router
from app.auth.rbac import PermissionDenied
from app.config import get_settings
from app.logging_utils import get_logger
from app.rag.parsers import ParseError
from app.services.metric_import import MetricImportError
from app.services.security import UploadSecurityError
from app.services.task_service import TaskConflict, TaskNotFound, TaskRevisionConflict

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    from app.services.runtime_safety import assert_runtime_safe

    # 必须早于建表/播种：生产配置不安全时，启动失败且数据库保持不变。
    assert_runtime_safe(settings)
    from app.db.seed import init_db, seed_all
    from app.services.task_dispatch import resolve_backend

    if settings.auto_init_db:
        init_db()
    if (
        settings.auto_init_db
        and settings.reconcile_stale_tasks
        and resolve_backend(settings) != "queue"
    ):
        # Queue tasks belong to independent workers. API restart must not mark
        # their pending/running work as failed; leases handle worker recovery.
        # 进程内线程池的任务无法跨进程存活：启动时把遗留的 running/pending 标记为中断失败，
        # 否则前端会永远显示"执行中"，用户既看不到错误也不知道可以重试。
        session = None
        try:
            from app.db.session import SessionLocal
            from app.services.task_service import reconcile_stale_tasks

            session = SessionLocal()
            interrupted = reconcile_stale_tasks(session)
            session.commit()
            if interrupted:
                logger.warning("启动时发现 %s 个被中断的任务，已标记为失败可重试", len(interrupted))
        except Exception as exc:  # 清理失败不阻塞启动
            logger.error("清理中断任务失败: %s", exc)
        finally:
            if session is not None:
                session.close()
    if settings.auto_init_db and settings.auto_seed:
        session = None
        try:
            from app.db.session import SessionLocal
            from app.models.identity import User

            session = SessionLocal()
            if session.query(User).count() == 0:
                logger.info("检测到空库，开始播种演示数据")
                seed_all(session)
        except Exception as exc:  # 播种失败不阻塞服务启动，但要醒目
            logger.error("播种演示数据失败: %s", exc)
        finally:
            if session is not None:
                session.close()

    logger.info(
        "服务已启动 env=%s database=%s llm=%s 执行后端=%s",
        settings.app_env,
        "sqlite" if settings.is_sqlite else "postgres",
        settings.llm_provider,
        settings.task_execution_backend,
    )

    # queue 后端下按配置内嵌一个 worker（本地开发省一个进程）；生产建议 WORKER_EMBEDDED=false
    embedded = None
    try:
        from app.worker import start_embedded_worker

        embedded = start_embedded_worker(settings)
        if embedded is not None:
            logger.info("已内嵌任务 worker（WORKER_EMBEDDED=true）；生产建议单独运行 python -m app.worker")
    except Exception as exc:  # worker 起不来不应该让 API 起不来
        logger.error("内嵌 worker 启动失败: %s", exc)

    # 外部告警独立走持久 outbox；默认关闭，启用后也不阻塞业务请求。
    try:
        from app.services.alert_delivery import start_alert_delivery_worker

        if start_alert_delivery_worker(settings) is not None:
            logger.info("已启动告警 Webhook 投递 worker")
    except Exception as exc:
        logger.error("告警 Webhook 投递 worker 启动失败: %s", exc)

    yield

    from app.services.workflow_runner import reset_runner
    from app.services.alert_delivery import reset_alert_delivery_worker
    from app.worker import reset_embedded_worker

    reset_embedded_worker()
    reset_alert_delivery_worker()
    reset_runner()
    logger.info("服务已关闭，工作流线程池与内嵌 worker 已停止")


def create_app() -> FastAPI:
    settings = get_settings()
    # 交互式接口文档（/docs、/redoc、/openapi.json）在**生产环境默认关闭**：
    # 它们会把全部端点、参数与数据结构暴露给任何能访问服务的人，等于免费给攻击者一张地图。
    # 本地/开发默认保留（调试与人工测试都要用），要显式重开可以设 EXPOSE_DOCS=true。
    expose_docs = settings.expose_docs and settings.app_env != "prod"
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=(
            "互联网企业产品智能诊断与需求决策平台：工作流优先、Agent 受控、证据驱动。"
            "报告区分事实/推断/假设/建议，关键结论可定位到文档或指标查询。"
        ),
        lifespan=lifespan,
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
        openapi_url="/openapi.json" if expose_docs else None,
    )

    origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):  # noqa: ANN001, ANN202
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        script_sources = "'self'" if settings.app_env == "prod" else "'self' https://cdn.jsdelivr.net"
        style_sources = (
            "'self' 'unsafe-inline'"
            if settings.app_env == "prod"
            else "'self' 'unsafe-inline' https://cdn.jsdelivr.net"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src {script_sources}; "
            f"style-src {style_sources}; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if settings.app_env == "prod":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    # ---------------- 统一错误结构 ----------------
    @app.exception_handler(TaskNotFound)
    async def _task_not_found(_request: Request, exc: TaskNotFound) -> JSONResponse:
        return JSONResponse(
            status_code=404, content={"code": "task_not_found", "message": str(exc)}
        )

    @app.exception_handler(TaskRevisionConflict)
    async def _task_revision_conflict(
        _request: Request, exc: TaskRevisionConflict
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content=exc.as_detail())

    @app.exception_handler(TaskConflict)
    async def _task_conflict(_request: Request, exc: TaskConflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"code": "task_conflict", "message": str(exc)})

    @app.exception_handler(PermissionDenied)
    async def _permission_denied(_request: Request, exc: PermissionDenied) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"code": "permission_denied", "message": str(exc), "detail": {"action": exc.action}},
        )

    @app.exception_handler(UploadSecurityError)
    async def _upload_rejected(_request: Request, exc: UploadSecurityError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"code": "upload_rejected", "message": str(exc), "detail": {"errors": exc.errors}},
        )

    @app.exception_handler(ParseError)
    async def _parse_error(_request: Request, exc: ParseError) -> JSONResponse:
        return JSONResponse(
            status_code=400, content={"code": "parse_failed", "message": str(exc)}
        )

    @app.exception_handler(MetricImportError)
    async def _metric_import_error(_request: Request, exc: MetricImportError) -> JSONResponse:
        return JSONResponse(
            status_code=400, content={"code": "metric_import_failed", "message": str(exc)}
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # 注意：`exc.errors()` 的 ctx 里可能带异常对象，必须转成纯文本再序列化
        details = [
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "type": str(error.get("type", "value_error")),
                "msg": str(error.get("msg", "")),
            }
            for error in exc.errors()[:10]
        ]
        return JSONResponse(
            status_code=422,
            content={
                "code": "invalid_request",
                "message": "请求参数校验未通过",
                "detail": {"errors": details},
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        logger.exception("未捕获异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "服务内部错误，请查看服务端日志"},
        )

    app.include_router(build_api_router(), prefix=settings.api_prefix)

    # 根路径保留兼容入口，并提供语义明确的存活/就绪探针。
    from app.api.routes_system import health as health_handler
    from app.api.routes_system import health_live as health_live_handler
    from app.api.routes_system import health_ready as health_ready_handler

    app.add_api_route("/health", health_handler, methods=["GET"], tags=["系统与审计"], summary="健康检查")
    app.add_api_route(
        "/health/live", health_live_handler, methods=["GET"], tags=["系统与审计"], summary="进程存活探针"
    )
    app.add_api_route(
        "/health/ready", health_ready_handler, methods=["GET"], tags=["系统与审计"], summary="关键依赖就绪探针"
    )

    # 前端静态资源（可选）：frontend/dist 存在时挂在根路径。
    # 必须放在所有 API 路由之后，否则 "/" 挂载会吞掉 /api/**。
    dist_dir = Path(settings.frontend_dist_dir)
    if dist_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="web")
        logger.info("已挂载前端静态资源：%s", dist_dir)
    else:
        logger.info("未发现前端构建产物（%s），仅提供 API 与 /docs", dist_dir)
    return app


app = create_app()

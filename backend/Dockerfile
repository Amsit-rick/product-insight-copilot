# =============================================================================
# 电商售后工单智能处理系统 / After-sales Ticket Copilot —— API 镜像
#
# 构建（必须以仓库根目录为 context，镜像会同时构建前端）：
#     docker build -f backend/Dockerfile -t pic-api:local .
# 或在 deploy 目录用 compose 构建：
#     docker compose -f deploy/docker-compose.yml up -d --build api
#
# 设计要点：
# - Node 阶段用 lockfile 可复现地构建前端，最终镜像不携带 Node 工具链；
# - 基础镜像 python:3.12-slim，与开发环境（py -3.12）保持一致；
# - 先装依赖再拷代码，最大化利用构建缓存；
# - 以非 root 用户运行（uid 10001），符合设计文档 11 节的安全要求；
# - 健康检查用 Python 标准库 urllib 访问 /health，不依赖 curl；
# - 模型密钥不进入镜像，全部由运行时环境变量注入。
# =============================================================================

ARG NODE_IMAGE=node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293
ARG PYTHON_IMAGE=python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

FROM ${NODE_IMAGE} AS frontend-build

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    FRONTEND_DIST_DIR=/app/frontend/dist \
    FORWARDED_ALLOW_IPS="" \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

# -----------------------------------------------------------------------------
# 系统依赖
# 当前 requirements-runtime.lock 全部依赖在 cp312 上都有 manylinux wheel
# （psycopg[binary] 自带 libpq，numpy/pandas/argon2-cffi 等均为二进制轮子），
# 因此默认不装编译工具链，镜像更小、构建更快。
# 只有在需要从源码编译某个包时，才取消下面这段注释：
# -----------------------------------------------------------------------------
# RUN apt-get update \
#     && apt-get install -y --no-install-recommends build-essential libpq-dev \
#     && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# Python 依赖（单独一层，代码改动不会触发重装）
# -----------------------------------------------------------------------------
COPY backend/requirements-runtime.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-runtime.lock

# -----------------------------------------------------------------------------
# 应用代码与数据库迁移
# migrations/ 里是本项目的**版本化 SQL 迁移**（001_init.sql 由 SQLAlchemy 元数据
# 编译生成，002_pgvector.sql 建向量扩展与 chunk_vectors 表），随镜像一起发布。
# 容器首次启动时会自动建表（AUTO_INIT_DB=true）；需要走正式迁移时执行：
#   docker compose -f deploy/docker-compose.yml exec api python -m app.db.migrate apply
# -----------------------------------------------------------------------------
COPY backend/app/ ./app/
COPY backend/sandbox_services/ ./sandbox_services/
COPY backend/migrations/ ./migrations/
# 发布前门禁必须能在最终镜像里直接执行，不能只存在于源码工作区。
# 事实探针计划只包含环境变量名和期望结构，不包含真实订单号、客户号或密钥。
COPY backend/scripts/preflight_check.py ./scripts/preflight_check.py
COPY backend/scripts/verify_ai_integrations.py ./scripts/verify_ai_integrations.py
COPY backend/scripts/verify_fact_integrations.py ./scripts/verify_fact_integrations.py
COPY backend/scripts/verify_webhook_integration.py ./scripts/verify_webhook_integration.py
COPY backend/scripts/verify_object_storage.py ./scripts/verify_object_storage.py
COPY backend/scripts/verify_target_environment.py ./scripts/verify_target_environment.py
COPY backend/scripts/verify_scheduler_failover.py ./scripts/verify_scheduler_failover.py
COPY backend/scripts/verify_metric_source.py ./scripts/verify_metric_source.py
COPY backend/scripts/reference_integration_api.py ./scripts/reference_integration_api.py
COPY backend/scripts/verify_reference_integration_sandbox.py ./scripts/verify_reference_integration_sandbox.py
COPY docs/integrations/fact-probe-plan.example.json ./docs/integrations/fact-probe-plan.example.json
COPY --from=frontend-build /build/frontend/dist ./frontend/dist/

# -----------------------------------------------------------------------------
# 运行时数据目录与非 root 用户
# config.py 以 BACKEND_DIR.parent 推导 PROJECT_ROOT（容器内为 /），
# 因此同时准备 /app/data（显式 STORAGE_LOCAL_DIR 指向这里）和 /data，
# 保证 STORAGE_BACKEND=local 的回退路径始终可写。
# -----------------------------------------------------------------------------
RUN set -eux; \
    groupadd --gid 10001 appuser; \
    useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash appuser; \
    mkdir -p /app/data/objects /data/objects; \
    chown -R appuser:appuser /app /data

# Keep release metadata after dependency and application layers so changing the
# commit SHA does not invalidate the expensive locked dependency installation.
ARG APP_RELEASE_SHA=unknown
LABEL org.opencontainers.image.title="after-sales-ticket-copilot" \
      org.opencontainers.image.revision="${APP_RELEASE_SHA}"

USER appuser

EXPOSE 8000

# -----------------------------------------------------------------------------
# 就绪检查：数据库等关键依赖不可用时返回非 2xx，避免继续接收流量。
# 使用 Python 标准库，避免依赖 curl/wget。
# 覆盖 8000 端口时，compose 侧可继续使用本检查（容器内始终是 8000）。
# -----------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4)"

# 生产建议改为多 worker：--workers 4（本镜像默认单进程便于开发调试）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

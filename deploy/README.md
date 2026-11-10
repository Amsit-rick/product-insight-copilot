# 部署与运行指南 —— 电商售后工单智能处理系统 / After-sales Ticket Copilot

本目录只放部署编排文件，业务代码在 `backend/`（FastAPI + LangGraph + SQLAlchemy）。

| 文件 | 作用 |
|---|---|
| `deploy/docker-compose.yml` | 默认编排：Postgres(pgvector) / Redis / 本地对象存储 / API / **worker**；MinIO、Qdrant 和只读主动异常调度器为可选 profile |
| `deploy/docker-compose.release.yml` | 发布覆盖：移除应用服务的源码构建，只接受 `name@sha256:digest` 镜像 |
| `backend/Dockerfile` | API 镜像（python:3.12-slim、非 root 用户、内置 `/health` 健康检查）；worker 复用同一镜像，但 Compose 会禁用不适用于 worker 的 HTTP 探针 |
| `deploy/docker-compose.ticket-load.yml` | 一次性工单生产形态负载覆盖：独立端口、stub 模型、本地对象存储、两个可扩容 worker |
| `backend/.env.example` | 全部环境变量模板，含中文说明 |
| `backend/pytest.ini` | pytest 配置（`testpaths=tests`、`pythonpath=.`、`addopts=-q`） |
| `scripts/dev.ps1` / `scripts/dev.sh` | 本地开发统一入口：`setup` / `seed` / `run` / `test` / `eval` / `lint` |
| `scripts/ci.ps1` | 一键 CI：后端全量测试 + 前端类型检查 + 构建 +（`-WithE2E`）浏览器冒烟；`-StrictPreflight` 可执行生产配置门禁 |

核心设计原则：**所有外部依赖都可降级**。不启动任何服务、不配置任何密钥，也能跑通主流程。

---

## 一、三种运行方式

### 方式一：Docker Compose 全栈（联调 / 验收）

前置：Docker Desktop 已启动；离线环境需要提前 `docker save` / `docker load` 相关镜像
（默认需要 `pgvector/pgvector:pg16`、`redis:7-alpine`、`python:3.12-slim`；MinIO 与 Qdrant 为可选 profile）。

```powershell
# 在项目根目录执行
docker compose -f deploy/docker-compose.yml up -d --build

# 查看状态与日志
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f api

# 停止（-v 会连数据卷一起删除）
docker compose -f deploy/docker-compose.yml down
```

启动前可只做静态编排校验，不启动容器也不触碰数据卷：

```powershell
docker compose -f deploy/docker-compose.yml config --quiet
```

该检查只能证明 Compose 文件和变量展开语法正确，不能证明镜像可拉取、服务健康或生产依赖可达。

目标节点发布不得使用 `--build`。先由 CI/构建机生成并推送镜像，再用 digest 引用验证和启动：

```cmd
set "PIC_API_IMAGE=registry.example.com/pic-api@sha256:<64位摘要>"
python backend\scripts\verify_release_compose.py --image "%PIC_API_IMAGE%"
docker compose -f deploy\docker-compose.yml -f deploy\docker-compose.release.yml up -d
```

`docker-compose.release.yml` 会从合并后的五个应用服务中删除 `build`，默认只拉取指定 digest。
本地离线验收可在已执行 `docker load` 后显式设置 `PIC_RELEASE_PULL_POLICY=never`，但这不等于制品已推送企业仓库。

默认启动顺序由健康检查驱动：`postgres` / `redis` 健康且 `migrate` 成功后，`api` 与
`worker` 才会启动。默认对象存储为本地卷、默认向量后端为 pgvector，因此不会拉取未启用的
MinIO 或 Qdrant。需要本地 MinIO 时使用 `--profile minio` 与 `STORAGE_BACKEND=minio`；
`minio-init` 使用同一固定镜像内的 `mc` 在服务健康后建桶。默认兼容镜像只用于本地集成验收，
生产应直接配置企业 S3 兼容端点，显式设置 `MINIO_ENDPOINT`、`MINIO_SECURE=true` 和独立最小权限账号，
且不要启动本地 `minio` profile。

### 任务执行与 worker（多副本）

`api` 与 `worker` **共用同一份环境变量**（compose 里的 YAML 锚点 `x-pic-env`）——
worker 与 API 的配置（模型路由、预算、密级、缓存 TTL…）一旦不一致，
问题会在上线后以很难查的方式暴露，所以只保留一份定义。

```powershell
# API 只入队，worker 执行；worker 可独立扩缩容
docker compose -f deploy/docker-compose.yml up -d --scale worker=3

# 看队列积压与断点恢复形态
curl http://127.0.0.1:8000/health   # execution 字段：backend / queue / checkpointer
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `TASK_EXECUTION_BACKEND` | `queue`（compose 内） | `inline` / `threadpool` / `queue`；容器编排按多副本设计，默认走队列 |
| `WORKER_EMBEDDED` | `false` | API 进程内是否内嵌 worker；生产保持 false，让故障域分开 |
| `WORKER_CONCURRENCY` | `2` | 单个 worker 并发处理的任务数 |
| `WORKER_LEASE_SECONDS` | `600` | 任务租约：worker 崩了超过这个时间未确认，任务会被回收重跑 |
| `WORKER_MAX_ATTEMPTS` | `3` | 单任务最多被领取执行几次，超过进 `dead` |
| `CHECKPOINTER_BACKEND` | `postgres`（compose 内） | 断点恢复存储：`memory` 单副本 / `sqlite` 同机多进程 / `postgres` 多副本；容器默认必须跨 API/worker 共享 |
| `CHECKPOINTER_POSTGRES_URL` | 复用业务 Postgres | 可指向独立 checkpoint 库；不设置时 Compose 自动使用 `postgres` 服务 |

> `langgraph-checkpoint-postgres` 已列入默认依赖。Compose 不再默认使用容器内 SQLite：
> API 与 worker 的容器文件系统彼此隔离，即使路径相同也不是共享 checkpoint。
> 断点恢复的差异有测试证明：`tests/integration/test_checkpointer_recovery.py`
> 用"换一个 runner 实例"模拟重启/换副本，`sqlite` 能恢复、`memory` 拿不到 checkpoint（走降级重建）。

工单生产形态负载验收（Windows CMD）：

```cmd
docker compose -p pic-ticket-load -f deploy\docker-compose.yml -f deploy\docker-compose.ticket-load.yml up -d --build --scale worker=2 postgres redis api worker
.venv\Scripts\python.exe backend\scripts\benchmark_ticket_queue.py --base-url http://127.0.0.1:18200 --count 24 --expected-workers 2 --json-out docs\testing\artifacts\ticket-production-load.json
docker compose -p pic-ticket-load -f deploy\docker-compose.yml -f deploy\docker-compose.ticket-load.yml down -v --remove-orphans
```

脚本会硬性检查 Postgres、健康 Redis、queue 执行、共享 Postgres checkpoint、stub 模型、
至少两个真实 `claimed_by`、P95、错误率和最终无 pending/claimed/dead；任一失败均返回非零退出码。

### 只读主动异常调度器（默认关闭）

`proactive-scheduler` 是独立于 API 和 worker 的单实例调度进程。它只读取标准化指标快照并生成
候选运行告警，不调用模型、不启动 Agent、不创建诊断任务，也不执行退款、改价或发送消息。
管理员仍需在系统状态页确认候选，确认后才会创建现有的多 Agent 诊断任务。

该服务同时受两层默认关闭保护：Compose 必须显式启用 `proactive` profile，且
`PROACTIVE_ANOMALY_ENABLED` 必须设为 `true`。多个调度容器被误启动时，数据库
`scheduler_leases` 租约只允许一个实例执行扫描，租约过期后其他实例可接管。

Windows CMD 启动与查看命令：

```cmd
cd /d D:\path\to\product-insight-copilot
set "PROACTIVE_ANOMALY_ENABLED=true"
set "PROACTIVE_ANOMALY_METRICS=["next_day_retention","crash_rate"]"
set "PROACTIVE_ANOMALY_MAX_DATA_AGE_DAYS=2"
docker compose -f deploy\docker-compose.yml --profile proactive up -d --build proactive-scheduler
docker compose -f deploy\docker-compose.yml --profile proactive ps proactive-scheduler
docker compose -f deploy\docker-compose.yml --profile proactive logs -f proactive-scheduler
```

停止调度进程不会删除候选告警或业务数据：

```cmd
docker compose -f deploy\docker-compose.yml --profile proactive stop proactive-scheduler
```

启用前必须确认指标列表、异常阈值、数据时效和扫描周期适合当前业务。默认只接受最新日期距离
UTC 今天不超过 2 天的数据；超期数据和未来日期数据会被跳过，不生成候选告警。默认周期为
300 秒，租约为 900 秒；租约应长于单轮最坏扫描耗时。管理员可在系统状态页查看最近运行时间、
候选数量、数据门禁跳过数量和错误标志，但页面不会暴露调度器 owner ID 或内部错误正文。

#### 标准化只读指标源

需要由调度器自动拉取真实指标时，额外配置 `METRIC_SOURCE_HTTP_ENABLED=true`、
`METRIC_SOURCE_HTTP_URL` 和服务端鉴权头。调度器会先读取白名单指标并通过现有导入校验写入
标准化快照，再执行异常扫描；同步失败时该租户本轮不会继续扫描旧缓存。完整请求、响应、
租户隔离和字段白名单见 `docs/integrations/metric-source-http-contract.md`。

不同指标的产出延迟可以单独配置。例如崩溃率要求 T+0、次日留存允许 T+2：

```cmd
set "PROACTIVE_ANOMALY_MAX_DATA_AGE_BY_METRIC={"crash_rate":0,"next_day_retention":2}"
```

发布前先运行默认不联网的探针；确认使用隔离测试租户后，再显式授权一次真实只读 GET：

```cmd
.venv\Scripts\python.exe backend\scripts\verify_metric_source.py --tenant-id <隔离测试租户ID> --json-output docs\testing\artifacts\metric-source-check.json
.venv\Scripts\python.exe backend\scripts\verify_metric_source.py --tenant-id <隔离测试租户ID> --allow-real-source-call --json-output docs\testing\artifacts\metric-source-check.json
```

证据不会保存 URL、鉴权头、请求 ID 或指标值。没有真实端点和测试租户时只能执行第一条，
不能把适配器实现完成等同于真实指标源已接通。

#### PostgreSQL 租约崩溃接管演练

发布镜像内置 `scripts/verify_scheduler_failover.py`。脚本会启动独立租约持有进程，证明第二个
进程在租约有效期内无法进入；随后强制终止持有者，确认竞争者只能在租约过期后接管。
演练使用随机租约名，只删除自己创建的租约行，不读取或修改业务表。

必须指向隔离的 PostgreSQL，并显式授权数据库调用。Windows CMD 示例：

```cmd
cd /d D:\path\to\product-insight-copilot
set "SCHEDULER_REHEARSAL_DATABASE_URL=postgresql+psycopg://<用户>:<密码>@127.0.0.1:<端口>/<数据库>"
.venv\Scripts\python.exe backend\scripts\verify_scheduler_failover.py --allow-real-database-call --lease-seconds 3 --json-out docs\testing\artifacts\scheduler-failover-local-check.json
```

不加 `--allow-real-database-call` 时只生成未执行计划，不连接数据库。证据不会保存数据库连接串、
租约名或 owner ID。即使本机 PostgreSQL 演练全部通过，结果也只标记
`local_postgres_failover_verified=true`、`cross_host_failover_verified=false` 和
`production_ready=false`；跨主机或托管 PostgreSQL 故障切换仍需目标环境单独验收。

### 真实售后事实接口

生产环境设置 `TICKET_USE_MOCK_ADAPTERS=false`，并配置订单、支付、物流、售后四个
`TICKET_FACT_*_URL`。URL 必须包含 `{order_id}`；鉴权头通过服务端 JSON 配置
`TICKET_FACT_AUTH_HEADERS` 注入。API 与 worker 从同一个 `x-pic-env` 继承配置。

上游标准响应、字段白名单和故障码见 `docs/integrations/fact-http-contract.md`。发布前执行
`backend\scripts\preflight_check.py --strict`；端点缺失或模板非法会直接失败。健康检查的
`fact_adapter` 字段只公开配置状态，不公开 URL 或鉴权头。

事实验收脚本随最终镜像发布。默认计划只保存环境变量名和期望结构，不保存真实订单号：

```powershell
..\.venv\Scripts\python.exe backend\scripts\verify_fact_integrations.py --validate-plan-only
..\.venv\Scripts\python.exe backend\scripts\verify_fact_integrations.py --allow-real-fact-adapters --runs 3 --json-out docs\testing\artifacts\fact-integration-result.json
```

第二条命令只有在显式授权后才联网，并要求测试订单号通过 `FACT_PROBE_*` 环境变量注入。
结果文件只记录字段名、故障类别、延迟和通过率，不记录订单号、客户号或事实值。

Webhook 验收同样分为本地闭环和真实最小投递：

```cmd
..\.venv\Scripts\python.exe backend\scripts\verify_webhook_integration.py --json-out docs\testing\artifacts\webhook-integration-local-check.json
..\.venv\Scripts\python.exe backend\scripts\verify_webhook_integration.py --require-production-config --allow-real-webhook-call --json-out docs\testing\artifacts\webhook-integration-result.json
```

第一条只访问本机回环地址并验证生产投递 worker 的签名、重试、死信和重放；第二条才会向已配置的
HTTPS 接收端发送一条不含生产数据的合成事件。两种证据都不保存 URL、HMAC 密钥或请求正文。

`after_sales_release_report.py` 只从 `docs/testing/artifacts/fact-integration-result.json`、
`webhook-integration-result.json` 和 `target-environment-result.json` 读取真实外部证据。探针输出包含带时区的
`generated_at`，活体证据最多接受 24 小时；复制本地回环结果、计划校验结果或手工改名不能解除外部门禁。

预发布报告和交付检查生成后必须打包为同一只读证据包：

```cmd
.venv\Scripts\python.exe backend\scripts\package_delivery_evidence.py --evidence-dir docs\testing\artifacts
```

脚本原子写入 `delivery-evidence-manifest.json`，绑定两份 JSON 的 SHA-256、各自生成时间和发布提交。
应用会拒绝缺 manifest、文件哈希变化、跨批次组合或发布 SHA 不一致的目录。生产部署应把 CI/交付系统
产出的独立目录配置到 `DELIVERY_EVIDENCE_HOST_DIR` 并只读挂载；仓库内默认目录仅用于本地演示，
避免出现“证据绑定某提交，但提交证据后 HEAD 又变化”的自引用问题。

GitHub Actions 的 `release-evidence` job 只在 backend、frontend、e2e 和 `production-shape` 全绿后
上传 `delivery-evidence-<完整提交SHA>`。制品包含报告、交付检查、manifest、脱敏验证摘要，以及
同一提交下远程 runner 生成的对象存储、运行形态和调度接管三份脱敏附件。下载并解压到部署主机后，
先在项目根目录复验：

```cmd
set APP_RELEASE_SHA=<制品名称中的完整提交SHA>
.venv\Scripts\python.exe backend\scripts\verify_delivery_evidence_bundle.py --evidence-dir D:\delivery-evidence\<提交SHA> --expected-release-sha %APP_RELEASE_SHA%
```

验证退出码为 0 后，再把同一目录配置为 `DELIVERY_EVIDENCE_HOST_DIR`。证据超过 24 小时、任一附件
被改、下载了其他提交的制品、批次不一致，或单 runner 附件错误声明生产就绪时都会失败；不得通过
手工修改 manifest 绕过。`remote_ci_evidence_complete=true` 不代表跨主机环境已验收。远程 job 只生成
和复验证据制品，不生成 `release-decision.json`，最终批准仍由授权角色独立完成。

生产启动另有不可绕过的运行时门禁：`APP_ENV=prod` 下若启用 `AUTO_SEED`、关闭
`LOGIN_ATTEMPT_RATE_LIMIT_PER_MINUTE`，或 JWT 密钥为空/过短/仍是占位值，API 会在建表前退出。

登录限流的故障策略：计数写入失败时返回 `503 login_temporarily_unavailable` 和
`Retry-After: 30`，不会继续验证密码或签发令牌。生产环境还要求实际计数后端为 Redis；
即使配置了 Redis，启动时降级到 SQLite/内存也会拒绝登录。Redis 在运行中短暂中断时，
连接恢复后的请求会重新尝试计数；若启动时已经降级，恢复 Redis 后需重启 API，
确认 `/health.cache.backend=redis`，再验证登录。已有令牌的业务请求不受这条登录门禁直接影响。
部署时应限制可信代理来源；应用不自行解析 `X-Forwarded-For`，但 ASGI 服务器可能改写客户端地址。
项目 Docker 镜像默认 `FORWARDED_ALLOW_IPS=`，本地开发与 CI 明确关闭代理头解析；接入网关时
应填写网关容器或负载均衡器的明确 IP/CIDR，并确保网关覆盖而不是透传客户端提供的转发头。

登录韧性演练（Windows CMD；目标须为隔离的生产配置 API 与可暂停的测试 Redis 容器）：

```cmd
.venv\Scripts\python.exe backend\scripts\verify_login_resilience.py --base-url http://127.0.0.1:18300 --redis-container <测试Redis容器名> --limit 2 --json-out docs\testing\artifacts\login-resilience.json
```

脚本验证伪造转发头无法拆分限流桶、Redis 暂停时登录 503、`/health` 标记运行时故障、
Redis 恢复后登录与健康状态恢复。脚本会执行 `docker pause/unpause`，只能指向隔离测试容器。

启动后可直接访问：

| 服务 | 地址 | 说明 |
|---|---|---|
| API 文档 | http://127.0.0.1:8000/docs | Swagger UI |
| 健康检查 | http://127.0.0.1:8000/health | 镜像 HEALTHCHECK 用的就是这个地址 |
| MinIO 控制台（可选 profile） | http://127.0.0.1:9001 | 仅在 `--profile minio` 且镜像、凭据已配置时可用 |
| Postgres | 127.0.0.1:5432 | 默认库/用户 `pic`，密码 `pic_dev_password` |
| Redis | 127.0.0.1:6379 | 无密码 |

端口都可用环境变量覆盖：`API_PORT`、`POSTGRES_PORT`、`REDIS_PORT`、`MINIO_PORT`、`MINIO_CONSOLE_PORT`。

**密钥与覆盖项**：compose 里的取值一律写成 `${LLM_API_KEY:-}` 这种透传形式，
仓库中不含任何真实密钥。需要注入时，在 `deploy/` 目录下自建 `.env`（compose 会自动读取
compose 文件所在目录的 `.env`，格式为 `KEY=VALUE`），或先在当前 PowerShell 会话导出环境变量：

```powershell
$env:LLM_PROVIDER = 'openai_compatible'
$env:LLM_API_KEY  = '<你的密钥，不要提交>'
docker compose -f deploy/docker-compose.yml up -d
```

### 方式二：本地零依赖（SQLite + 进程内缓存 + 本地文件存储 + stub 模型）

不需要 Docker，不需要任何数据库或对象存储服务：

```powershell
scripts\dev.ps1 setup     # 创建 .venv 并按 Windows 哈希锁安装依赖
scripts\dev.ps1 run       # 启动 http://127.0.0.1:8000/docs
```

```bash
chmod +x scripts/dev.sh   # 首次
./scripts/dev.sh setup
./scripts/dev.sh run
```

数据落位（自动创建，无需手工建目录）：

| 内容 | 位置 | 相关变量 |
|---|---|---|
| 业务库 | `<项目根>\data\pic.db` | `DATABASE_URL`（不设置即为该默认值） |
| 原始文档 / 解析结果 | `<项目根>\data\objects` | `STORAGE_LOCAL_DIR` |
| 演示数据（随仓库提供） | `backend\data\demo\documents` | `DEMO_DATA_DIR` |

此时生效的默认值为：`VECTOR_BACKEND=auto`（SQLite 下自动退化为 Python 余弦相似度）、
`CACHE_BACKEND=auto`（Redis 缺失时退化为进程内缓存）、`STORAGE_BACKEND=local`、
`LLM_PROVIDER=stub`、`EMBEDDING_PROVIDER=stub`（离线确定性输出，适合开发与测试）。

### 方式三：混合模式（应用在宿主机 + 基础设施在容器）

日常开发最省事的组合：数据库/缓存/对象存储跑容器，代码在宿主机热重载。

```powershell
# 1) 只起基础设施（api 服务不启动）
docker compose -f deploy/docker-compose.yml --profile minio up -d postgres redis minio minio-init
docker compose -f deploy/docker-compose.yml --profile minio ps

# 2) 准备本地配置
Copy-Item backend\.env.example backend\.env

# 3) 编辑 backend\.env，改用宿主机视角的地址（容器内是服务名，宿主机是 localhost）
#    DATABASE_URL=postgresql+psycopg://pic:pic_dev_password@localhost:5432/pic
#    VECTOR_BACKEND=pgvector
#    REDIS_URL=redis://localhost:6379/0
#    CACHE_BACKEND=redis
#    STORAGE_BACKEND=minio
#    MINIO_ENDPOINT=localhost:9000
#    MINIO_ACCESS_KEY=picminio
#    MINIO_SECRET_KEY=picminio123

# 4) 启动应用
scripts\dev.ps1 run
```

命令入口与端口速查（宿主机 `localhost` ↔ 容器服务名）：

| 依赖 | 容器内主机名 | 宿主机地址 |
|---|---|---|
| Postgres | `postgres:5432` | `localhost:5432` |
| Redis | `redis:6379` | `localhost:6379` |
| MinIO | `minio:9000` | `localhost:9000` |

---

## 二、环境变量分组说明

变量名 = `backend/app/config.py` 中字段名的大写形式（大小写不敏感，`case_sensitive=False`），
完整清单与逐项中文注释见 `backend/.env.example`。分组如下：

| 分组 | 变量（示例） | 控制什么 |
|---|---|---|
| 基础 | `APP_NAME` `APP_ENV` `DEBUG` `API_PREFIX` | 应用名、环境标识（local/dev/test/prod）、调试开关、接口前缀 |
| 数据库 | `DATABASE_URL` `DB_ECHO` `DB_POOL_SIZE` `DB_MAX_OVERFLOW` | 连接串；SQLite 与 Postgres 共用同一套代码，`postgresql+psycopg://` 走 psycopg3 |
| 向量 | `VECTOR_BACKEND` `EMBEDDING_DIM` `VECTOR_TABLE_NAME` | 用 pgvector 还是 Python 兜底；维度必须与 Embedding 模型一致（BGE-M3 = 1024） |
| 缓存 | `REDIS_URL` `CACHE_BACKEND` `CACHE_TTL_SECONDS` | 幂等键/限流/缓存；`auto` 时 Redis 不可用自动退化为进程内缓存（`/health` 会给出 `healthy: false` 与 `degraded_reason`，不会假装在用 Redis）。**多副本部署时限流与缓存必须走 Redis**，否则各副本各算各的 |
| 对象存储 | `STORAGE_BACKEND` `STORAGE_LOCAL_DIR` `MINIO_*` | `local` 落盘目录或 MinIO 端点、密钥、桶名、是否 HTTPS |
| 认证 | `JWT_SECRET` `JWT_ALGORITHM` `JWT_EXPIRE_MINUTES` `PASSWORD_SCHEME` | 令牌密钥与有效期、口令哈希方案；生产启动会拒绝默认/占位/短密钥 |
| 模型网关 | `LLM_PROVIDER` `LLM_BASE_URL` `LLM_API_KEY` `LLM_MODEL` `LLM_TIMEOUT_SECONDS` `LLM_MAX_RETRIES` `LLM_TEMPERATURE` `LLM_MAX_OUTPUT_TOKENS` | OpenAI 兼容协议网关；DeepSeek / Qwen / GLM 只换 `LLM_BASE_URL` 与模型名 |
| 多模型路由 / 灰度 | `AGENT_MODEL_OVERRIDES` `LLM_FALLBACK_MODELS` `MODEL_CANARY` `MODEL_CANARY_SALT` | 按 Agent/节点覆盖模型与端点；主模型失败时的降级链；按任务百分比把流量切到候选模型（见项目 README §3.3、§3.4） |
| 成本与预算 | `MODEL_PRICES` `MODEL_PRICE_DEFAULT` `CURRENCY` `TASK_TOKEN_LIMIT` `TASK_COST_LIMIT` `TENANT_DAILY_COST_LIMIT` `BUDGET_EXCEEDED_ACTION` | 单价表与兜底价、任务/租户预算上限、超限后是降级出报告还是直接失败 |
| 并发 / 限流 / 缓存 TTL | `MODEL_MAX_CONCURRENCY` `MODEL_CONCURRENCY_TIMEOUT_SECONDS` `TASK_SUBMIT_RATE_LIMIT_PER_MINUTE` `LOGIN_ATTEMPT_RATE_LIMIT_PER_MINUTE` `RETRIEVAL_CACHE_TTL_SECONDS` `METRIC_CACHE_TTL_SECONDS` `RECONCILE_STALE_TASKS` | 模型并发闸门、建任务/登录限流、检索/指标缓存 TTL、进程重启后残留任务对账 |
| Embedding | `EMBEDDING_PROVIDER` `EMBEDDING_BASE_URL` `EMBEDDING_API_KEY` `EMBEDDING_MODEL` | 向量化服务；为空可用 `stub` 离线向量 |
| 工作流 / Agent | `AGENT_TIMEOUT_SECONDS` `AGENT_MAX_RETRIES` `AGENT_MAX_TOKENS` `AGENT_MAX_TOOL_CALLS` `WORKFLOW_MAX_RETRIEVE_ROUNDS` `*_VERSION` | 受控执行上限与可追溯版本号（工作流/Prompt/Embedding/索引） |
| 报告与人工审核 | `REPORT_MAX_EVIDENCE` `EVIDENCE_MIN_RELEVANCE` `CONFLICT_SIMILARITY_THRESHOLD` `RISK_SCORE_HIGH_THRESHOLD` `CONFIDENCE_LOW_THRESHOLD` `MIN_EVIDENCE_COUNT` | 证据条数上限、噪声阈值、冲突判定、触发人工审核的阈值 |
| 指标导入 / 演示数据 | `METRIC_IMPORT_MAX_ROWS` `METRIC_RATIO_BOUNDS` `DEMO_DATA_DIR` | 导入行数上限、比例类指标合理上界、播种与评测使用的演示数据目录 |
| 上传与内容安全 | `MAX_UPLOAD_MB` `ALLOWED_UPLOAD_EXTENSIONS` `PROMPT_INJECTION_PATTERNS` `SENSITIVE_OUTPUT_PATTERNS` | 上传限制；提示词注入命中即转人工审核并记审计；输出敏感词检查 |
| 检索 | `RETRIEVAL_TOP_K` `RETRIEVAL_CANDIDATE_K` `CHUNK_SIZE` `CHUNK_OVERLAP` `RERANKER_ENDPOINT` `RERANKER_TIMEOUT_SECONDS` | 召回/候选条数、切分粒度、可选 BGE-Reranker 服务 |
| 任务执行 | `TASK_WORKER_THREADS` `TASK_POLL_INTERVAL_SECONDS` | 后台线程数、任务轮询与中断检测间隔 |
| 主动异常调度 | `PROACTIVE_ANOMALY_ENABLED` `PROACTIVE_ANOMALY_METRICS` `PROACTIVE_ANOMALY_MAX_DATA_AGE_DAYS` `PROACTIVE_ANOMALY_MAX_DATA_AGE_BY_METRIC` `PROACTIVE_ANOMALY_INTERVAL_SECONDS` `PROACTIVE_ANOMALY_LEASE_SECONDS` | 默认关闭的只读扫描、指标白名单、全局/分指标数据最大延迟、扫描周期和数据库单实例租约 |
| 只读指标源 | `METRIC_SOURCE_HTTP_ENABLED` `METRIC_SOURCE_HTTP_URL` `METRIC_SOURCE_HTTP_AUTH_HEADERS` `METRIC_SOURCE_HTTP_TIMEOUT_SECONDS` `METRIC_SOURCE_HTTP_MAX_RETRIES` `METRIC_SOURCE_HTTP_MAX_ROWS` `METRIC_SOURCE_HTTP_MAX_RESPONSE_BYTES` | 调度前通过固定 GET 端点读取标准化指标；默认关闭，生产要求 HTTPS，地址和鉴权信息不回显 |

安全约定（对应设计文档 11 节）：模型密钥只存在于服务端环境变量，禁止写入前端代码、
接口响应或普通日志；`backend/.env` 与 `deploy/.env` 都不应提交到版本库。

---

## 三、导入种子数据 / 演示数据

仓库自带 9 篇演示文档（PRD、指标字典、评审会纪要、客服工单、应用商店评论、发布说明、
事故复盘、运营指标与 A/B 实验），位于 `backend\data\demo\documents`。

1. **一键播种（若仓库提供了播种脚本）**

   ```powershell
   scripts\dev.ps1 seed        # Windows
   ./scripts/dev.sh seed       # macOS / Linux / Git Bash
   ```

   脚本按以下顺序自动寻找播种入口：`backend\app\seed.py`、`backend\app\db\seed.py`、
   `backend\scripts\seed.py`、`backend\scripts\seed_demo.py`（以及 `backend` 下任意 `seed*.py`）。
   **当前仓库还没有播种脚本**，此时该命令会以中文明确提示期望的脚本路径后退出——这是预期行为，
   不是环境故障。

2. **走接口手工导入（任何运行方式都适用）**

   ```text
   POST /api/v1/documents                     上传文档（multipart，受 MAX_UPLOAD_MB 与扩展名白名单限制）
   POST /api/v1/metrics/import                导入指标数据（CSV / Excel，受 METRIC_IMPORT_MAX_ROWS 限制）
   POST /api/v1/knowledge-bases/{id}/reindex  重建索引（重新切分 + 向量化）
   POST /api/v1/tasks                         创建分析任务
   GET  /api/v1/reports/{report_id}           获取报告
   POST /api/v1/evaluations/run               运行 AI 评测
   ```

   完整清单见 Swagger：http://127.0.0.1:8000/docs

3. **容器内的演示数据**：`api` 服务把 `backend/data/demo` 只读挂载到 `/app/data/demo`，
   并把 `DEMO_DATA_DIR` 指向该目录，因此在容器里做本地存储/播种时同样能读到演示数据。

初始索引建议：文档导入后调用一次 reindex，确保检索与引用链路完整可用。

---

## 四、测试与评测

```powershell
scripts\dev.ps1 test          # 全部测试
scripts\dev.ps1 test tests\unit -v
scripts\dev.ps1 eval          # 离线评测集（backend\tests\evaluation）
scripts\dev.ps1 lint          # ruff；未安装时退化为 python -m compileall 语法检查
```

```bash
./scripts/dev.sh test
./scripts/dev.sh eval
```

以上命令都在 `backend/` 目录下执行 pytest，因此会读取 `backend/pytest.ini` 的
`testpaths = tests`、`pythonpath = .`、`addopts = -q`；测试为同步用例（`fastapi.testclient`），
不依赖 `pytest-asyncio`。测试与评测默认使用本地 SQLite，因此不需要启动任何容器。

评测集位置与覆盖范围（对应设计文档 16.2）：`backend/tests/evaluation`，
覆盖单知识库问题、跨知识库问题、无答案问题、冲突证据、版本时间错配、数据缺失、
越权请求、Prompt 注入；指标包含检索命中率、引用正确率、证据充分率、拒答准确率、
人工接管率、平均延迟等。评测用例说明见 `backend/tests/evaluation/cases_README.md`。

---

## 五、常见问题

**1. 提示缺少项目依赖**
`scripts\dev.ps1` 会先探测 `import fastapi`，失败时直接给出中文错误与解决命令。
本机若无法访问 PyPI，可用离线 wheel 目录安装：

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes --no-index --find-links <wheel目录> -r backend\requirements-dev-windows.lock
```

**2. 端口被占用**
`scripts\dev.ps1 run 8080` 可换端口；容器方式改 `API_PORT` 即可。

**3. PowerShell 提示"在此系统上禁止运行脚本"**
这是 Windows 的执行策略限制，与本项目无关。两种处理方式：

```powershell
# 方式 A：单次绕过（不改动系统设置）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev.ps1 run

# 方式 B：为当前用户放开一次
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

macOS / Linux / Git Bash 下若提示权限不足，执行一次 `chmod +x scripts/dev.sh`。

**4. MinIO 健康检查失败**
compose 通过 `curl -fsS http://127.0.0.1:9000/minio/health/live` 探活，并由 `minio-init`
创建桶。先执行 `docker compose -f deploy\docker-compose.yml --profile minio ps`，再查看
`docker compose -f deploy\docker-compose.yml --profile minio logs minio minio-init`。若使用自定义镜像，
必须确认镜像包含 `curl`，并自行验证数据目录、入口命令和客户端路径兼容，不能只凭静态配置判定可用。

**5. 镜像构建失败：`COPY migrations/` 找不到源目录**
`backend/Dockerfile` 按约定拷贝 `app/` 与 `migrations/`，两者都必须存在于构建上下文中。
若仓库尚未生成 alembic 迁移目录，请先在 `backend/` 下初始化并提交迁移脚本后再构建。

**6. 数据库建表 / 迁移**
业务库结构与迁移脚本由 `backend/migrations`（alembic）与项目提供的 alembic 配置管理；
若项目提供了 alembic 配置，可在 `backend/` 目录执行 `python -m alembic upgrade head` 完成建表。
本地 SQLite 模式下应用会按需创建所需目录与文件。

**7. 完全离线环境**
Docker 方式需提前准备好全部镜像（`docker save` / `docker load`）；
Python 依赖需准备离线 wheel 目录。本地零依赖模式（方式二）不需要网络，也不需要镜像。

---

## 六、已验证范围与剩余限制

2026-11-07 已在 Windows Docker Desktop 的隔离 Compose project 中完成真实镜像构建和运行验证：

- `migrate` 一次性容器先应用 12/12 个版本化迁移，API 与 worker 仅在迁移成功后启动；
- 真 PostgreSQL + Redis + pgvector + API + 两个独立 worker 运行，`/health/ready` 返回 ready；
- 24/24 工单完成，0 错误，两个 worker 均实际领取任务，最终无 pending / claimed / dead；
- 多进程专项 22/22、真 Worker 容器崩溃接管 10/10、死信与重放 22/22；
- 独立 PostgreSQL 自定义格式快照恢复成功，12 条迁移记录、`tickets` 表和探针数据均恢复。
- 本地 MinIO profile 健康并成功创建 `pic-documents`；`/health/ready` 返回 `storage.ok=true`、
  `storage.shared=true`；API 写入、Worker-2 读取、API 重启后读取、Worker-3 删除、API 确认删除全部通过。
- 对象存储证据在 `docs/testing/artifacts/object-storage-local-check.json`，只记录布尔检查、字节数和耗时，
  不包含端点、密钥、对象键或正文，并明确 `production_ready=false`。
- GitHub Actions 已增加 `production-shape` job：在远程单 runner 启动 PostgreSQL、Redis、MinIO、API 和两个 Worker，
  执行共享存储 put/get/delete 与脱敏目标探针并上传证据。当前只完成本地等价模拟，尚未由仓库 push 实际触发。
- 目标环境验收使用 `backend/scripts/verify_target_environment.py`。默认不联网；真实检查必须显式授权，并且至少两个生产节点
  的 `/health/ready` 全部通过、操作者确认不同主机后，才可标记跨主机目标环境已验证。
- 主动异常调度器已在本机隔离 PostgreSQL 上完成独立进程竞争、强制崩溃、租约到期接管和租约行清理演练；
  脱敏证据为 `docs/testing/artifacts/scheduler-failover-local-check.json`。GitHub Actions 已加入同类单 runner 演练定义，
  但在仓库实际 push 触发前不能声称远程 runner 已通过，更不能把单 runner 证据当成跨主机接管。
- 交付检查与交付摘要现直接读取 `infrastructure_verification` 中的目标环境状态，不再错误沿用本机 Docker 字段。
- 事实平台门禁只有在真实 HTTP 适配器至少运行 3 轮、四类来源全通过、场景一致且延迟门槛通过时解除；
  消息渠道门禁只有在 HTTPS/HMAC 配置、本地投递契约和一次真实外部送达全部通过时解除。
- 真实客服测试使用 `backend/scripts/validate_usability_test_record.py`。空模板、自动化回归、开发自测不会解除门禁，
  且任何参与者原话或客户敏感信息都不应写入上传证据。
- 真实模型发布门禁读取 `docs/testing/artifacts/real-model-holdout-result.json`。该证据必须来自至少 200 条
  独立人工标注脱敏 holdout、真实模型和真实事实接口的至少 3 轮运行，并由业务负责人明确通过率、
  一致性、证据支持率和 P95 四项阈值；证据不保存逐条 case 详情，超过 168 小时即失效。
- 即使所有机器门禁通过，也必须按 `docs/testing/release-decision.md` 由授权角色形成结构化批准。
  批准与当前提交及证据摘要绑定，最长有效 24 小时；CI 不生成 approve，证据变化后旧批准自动失效。

这轮使用 stub 模型、mock 售后事实和本机 MinIO，只证明容器、迁移、队列、Redis、pgvector、共享对象存储与恢复链路；
不代表真实模型质量、真实订单/支付/物流/售后接口、企业对象存储、外部 HTTPS Webhook 或跨主机编排已经投产。
当前隔离负载原始数据见 `docs/testing/artifacts/ticket-production-load-20261106.json`。

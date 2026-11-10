# 发布清单 —— 电商售后工单智能处理系统 / After-sales Ticket Copilot

这份清单的目的不是"再读一遍文档"，而是**把上线前该确认的事变成可执行的动作与可判定的结果**。
能机器判断的部分都在 `backend/scripts/preflight_check.py` 里，先跑它，再手工过剩下的条目。

```powershell
cd backend
..\.venv\Scripts\python.exe scripts\preflight_check.py              # 常规自检（当前 25 项）
..\.venv\Scripts\python.exe scripts\preflight_check.py --strict     # 生产标准：风险项升格为 FAIL
..\.venv\Scripts\python.exe scripts\preflight_check.py --json       # 给流水线消费
..\.venv\Scripts\python.exe scripts\preflight_check.py --with-eval  # 附带跑 8 条离线评测
..\.venv\Scripts\python.exe scripts\benchmark_policy_retrieval.py --json-out .tmp\policy-retrieval-benchmark.json  # 政策 Recall@5 门禁（stub，零成本）
..\.venv\Scripts\python.exe scripts\verify_ai_integrations.py       # 只校验模型/Embedding/Reranker 配置，不联网
..\.venv\Scripts\python.exe scripts\verify_ai_integrations.py --allow-real-ai-calls --json-out ..\docs\testing\artifacts\ai-integration-result.json
..\.venv\Scripts\python.exe scripts\verify_fact_integrations.py --validate-plan-only
..\.venv\Scripts\python.exe scripts\verify_fact_integrations.py --allow-real-fact-adapters --runs 3 --json-out ..\docs\testing\artifacts\fact-integration-result.json
..\.venv\Scripts\python.exe scripts\verify_metric_source.py --tenant-id <隔离测试租户ID> --json-output ..\docs\testing\artifacts\metric-source-local-check.json
..\.venv\Scripts\python.exe scripts\verify_metric_source.py --tenant-id <隔离测试租户ID> --allow-real-source-call --runs 3 --max-latency-ms 5000 --json-output ..\docs\testing\artifacts\metric-source-result.json
..\.venv\Scripts\python.exe scripts\verify_webhook_integration.py --json-out ..\docs\testing\artifacts\webhook-integration-local-check.json
..\.venv\Scripts\python.exe scripts\verify_webhook_integration.py --require-production-config --allow-real-webhook-call --json-out ..\docs\testing\artifacts\webhook-integration-result.json
..\.venv\Scripts\python.exe scripts\verify_object_storage.py
..\.venv\Scripts\python.exe scripts\verify_object_storage.py --allow-real-storage-calls --action full --probe-id release-storage-check --json-out ..\docs\testing\artifacts\object-storage-result.json
..\.venv\Scripts\python.exe scripts\verify_target_environment.py --node api-a=https://api-a.example.com --node api-b=https://api-b.example.com --expected-env prod --allow-real-target-calls --confirm-distinct-hosts --json-out ..\docs\testing\artifacts\target-environment-result.json
..\.venv\Scripts\python.exe scripts\validate_usability_test_record.py --input ..\docs\product-redesign\客服可用性测试记录.json --summary-out ..\docs\testing\artifacts\usability-test-summary.json
..\.venv\Scripts\python.exe scripts\after_sales_release_report.py --policy-benchmark-evidence .tmp\policy-retrieval-benchmark.json --json-out ..\docs\testing\artifacts\after-sales-release-readiness.json
```

项目根目录也可以使用统一入口执行严格配置门禁：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ci.ps1 -StrictPreflight -SkipFrontend
# 没有 Docker CLI 时才显式跳过 Compose 静态校验（会在输出中标记为 skipped）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ci.ps1 -SkipCompose -SkipFrontend
```

`-StrictPreflight` 只切换 preflight 档位，不会自动修改环境变量；当前本地开发配置预期会失败，失败结果代表生产配置尚未注入，不代表代码回归失败。普通 CI 还会执行 Compose 静态校验；`-SkipCompose` 只能在没有 Docker CLI 时使用。

预发布报告只接受固定文件名的真实证据：`fact-integration-result.json`、`webhook-integration-result.json`、
`target-environment-result.json`。三类活体证据必须含带时区的 `generated_at` 且不超过 24 小时；本地回环、
计划校验、单节点测试环境、少于 3 轮的事实探针或畸形 JSON 均保持 `blocked_external`。

退出码：有 FAIL → 1。**发布门禁就以这个退出码为准**，不要靠肉眼看输出。

---

## 0. 一句话原则

> 发布不是"把代码放上去"，而是"让线上状态可以被解释、可以被回滚"。
> 因此清单里每一项都对应一个具体风险：**谁会发现、多久发现、怎么退**。

---

## 1. 代码与质量门（发布前必须全绿）

| # | 检查 | 命令 / 依据 |
|---|---|---|
| 1.1 | 后端全量测试通过 | `scripts\ci.ps1`（或 `pytest tests -q`）；以本次命令退出码和实际收集数量为准，不在文档中固化旧数量 |
| 1.2 | 前端类型检查与构建通过 | `scripts\ci.ps1`（tsc + vite build） |
| 1.3 | 浏览器端冒烟通过 | `scripts\ci.ps1 -WithE2E`（自起 stub 后端 + 无头 Chrome，通用冒烟 + 售后角色回归） |
| 1.4 | 迁移 DDL 与模型一致 | `pytest tests/unit/test_migrate.py`（漏表会 FAIL） |
| 1.5 | 离线评测基线未退化 | `pytest tests/evaluation -q`；对照 README §8 的通过率与分项 |
| 1.6 | 无密钥入库 | `preflight_check.py` 的"密钥入库"项 + `git grep sk- HEAD` 应为空 |
| 1.7 | 本次改动已提交并推送 | `git status` 干净、`git rev-parse HEAD` == `origin/main` |

> 1.4 不是形式主义：开发时我漏把队列表写进 `001_init.sql`，正是这条测试拦下来的——
> 否则 Postgres 部署会少一张表，直到有人点"重试任务"才炸。

---

## 2. 配置（生产必改项）

对照 `backend/.env.example` 与 `deploy/.env.example`，逐项确认：

| # | 配置 | 生产要求 | 不改的后果 |
|---|---|---|---|
| 2.1 | `APP_ENV` | `prod` | 预检不会按生产标准判定 |
| 2.2 | `DEBUG` | `false` | 错误详情外泄 |
| 2.3 | `JWT_SECRET` | 自定义、≥32 字节随机值 | 任何人都能签发合法令牌 |
| 2.4 | `CORS_ORIGINS` | 真实前端域名，不含 `*` 与本地地址 | 任意站点可调用接口 |
| 2.5 | `LLM_PROVIDER` + `LLM_API_KEY` | 真实模型；密钥只从环境变量注入 | 静默降级为 stub，报告里没有模型结论 |
| 2.6 | `MODEL_PRICES` / `MODEL_PRICE_DEFAULT` | 按供应商当前价目覆盖 | 成本数字不可信 |
| 2.7 | 预算三件套 | 至少设一个（`TASK_TOKEN_LIMIT` / `TASK_COST_LIMIT` / `TENANT_DAILY_COST_LIMIT`） | 异常任务把额度烧穿 |
| 2.8 | `BUDGET_EXCEEDED_ACTION` | `degrade`（出报告但不用模型）或 `abort`（明确失败） | 按业务口径选，别留默认值不问 |
| 2.9 | `TASK_SUBMIT_RATE_LIMIT_PER_MINUTE` | >0，且 `CACHE_BACKEND=redis` | 多副本下限流形同虚设 |
| 2.10 | `LOGIN_ATTEMPT_RATE_LIMIT_PER_MINUTE` | >0，且 `CACHE_BACKEND=redis` | 登录接口可被暴力尝试 |
| 2.11 | `AUTH_COOKIE_SECURE` / `AUTH_COOKIE_SAMESITE` | HTTPS 生产环境必须 `true`；SameSite 按跨站部署方式确认，默认 `lax` | 会话 Cookie 经明文发送，或浏览器不按预期携带会话 |
| 2.12 | Cookie / CSRF 名称 | `AUTH_COOKIE_NAME`、`AUTH_CSRF_COOKIE_NAME` 非空且不同；`AUTH_CSRF_HEADER_NAME` 非空 | CSRF 双提交校验失效或会话不可用 |
| 2.13 | `MODEL_MAX_CONCURRENCY` | 按供应商配额设置 | 打满限流、批量降级 |
| 2.14 | `DATABASE_URL` | Postgres（多副本必须） | SQLite 单文件无法多副本共享 |
| 2.15 | `CACHE_BACKEND` / `REDIS_URL` | `redis` 并确认连通 | 缓存与限流退化为"每个副本各算各的" |
| 2.16 | `STORAGE_BACKEND` / `MINIO_*` | `minio`（或企业 S3 兼容存储），HTTPS、独立最小权限账号、桶已预创建 | 单机落盘、明文传输或管理员密钥扩大故障与泄露范围 |
| 2.17 | `FORWARDED_ALLOW_IPS` | 直连时为空；经网关时为明确代理 IP/CIDR，禁止 `*` | 攻击者伪造来源 IP 绕过登录限流 |
| 2.18 | 事实验收计划 | `preflight_check.py` 必须 PASS；计划只引用环境变量，不得写入订单号/客户号 | 真实联调前计划损坏或泄露敏感标识 |
| 2.19 | `EMBEDDING_PROVIDER` + 端点/密钥 | 真实 BGE-M3 或兼容 Embedding 服务 | 哈希向量只能证明流程可跑，不能代表语义召回质量 |
| 2.20 | `RERANKER_ENDPOINT` | 可访问的 BGE-Reranker HTTP(S) 服务 | 检索只能使用启发式重排，质量不可按生产标准验收 |
| 2.21 | `ALERT_WEBHOOK_*` | 启用 HTTPS Webhook 并配置 HMAC 密钥 | 严重告警只留在站内，外部值班系统无法确认来源和及时处置 |

真实 AI 服务配置完成后，必须运行 `verify_ai_integrations.py --allow-real-ai-calls`。该脚本只发出一次最小 LLM、Embedding 和 Reranker 请求，证据文件不保存回答正文、向量值、候选原文、端点或密钥；Embedding 返回维度必须与 `EMBEDDING_DIM` 一致。

**预检已覆盖 2.2–2.21 的机器判定**；2.1 与 2.6 需要人确认（价格与业务口径机器判断不了）。

---

## 3. 多副本形态（`README §7.4`）

| # | 检查 | 期望 |
|---|---|---|
| 3.1 | `TASK_EXECUTION_BACKEND` | `queue`（多副本）；`threadpool` 只允许单副本 |
| 3.2 | `WORKER_EMBEDDED` | `false`：worker 与 API 故障域分开 |
| 3.3 | worker 进程 | 至少 1 个在跑：`docker compose ... ps` 或 `python -m app.worker` |
| 3.4 | `WORKER_LEASE_SECONDS` | 大于单任务最长执行时间（真实模型下建议 ≥600s） |
| 3.5 | `CHECKPOINTER_BACKEND` | `prod` 必须为 `postgres`；`sqlite` 仅允许本地/开发同机多进程，**不要用于生产** |
| 3.6 | `/health` 的 `execution` | `checkpointer.shared=true`、`queue.pending` 不持续增长、`queue.dead=0` |
| 3.7 | worker 扩容验证 | `--scale worker=2` 后同一任务不被执行两次（队列用条件更新保证） |

> 3.5 的差别有测试证明（`tests/integration/test_checkpointer_recovery.py`）：
> 换一个 runner 实例模拟重启/换副本，`sqlite` 能恢复、`memory` 拿不到 checkpoint。
> 留 memory 的后果是"人工审核结论提交后走到降级重建路径"——不算丢数据，但报告质量会掉。

---

## 4. 安全复核

| # | 检查 | 说明 |
|---|---|---|
| 4.1 | 密级策略已按企业口径配置 | `access_policies` 表：指标/指标域白名单、有效期、时间窗、行级范围（`README §7.3`） |
| 4.2 | 指标级策略有兜底 | 未配置策略的指标默认**放行**：敏感指标必须显式加策略，别指望默认拦 |
| 4.3 | 上传限制 | `MAX_UPLOAD_MB`、`ALLOWED_UPLOAD_EXTENSIONS` 按业务确认 |
| 4.4 | 注入与敏感词 | `PROMPT_INJECTION_PATTERNS`、`SENSITIVE_OUTPUT_PATTERNS` 是否覆盖业务术语 |
| 4.5 | 审计可用 | 管理员可查 `GET /api/v1/audit-logs`，且包含登录、任务、文档、导入、审批 |
| 4.6 | 演示账号 | `AUTO_SEED=false`；生产启动门禁会阻止自动创建 `admin/admin123` 等演示账号 |
| 4.7 | 浏览器会话 | Web 登录只调用 `/auth/session`，响应不得含 `access_token`；会话 Cookie 为 Secure + HttpOnly，写请求必须通过 CSRF 双提交；`/auth/login` 仅供 Bearer API 客户端 |

---

## 5. 数据与迁移

| # | 检查 | 命令 / 说明 |
|---|---|---|
| 5.1 | 备份/恢复 | 发布前生成 Postgres 自定义格式快照，并在隔离库执行 `pg_restore --exit-on-error`；本地可先跑 `scripts\rehearse-postgres-restore.ps1` 验证流程 |
| 5.2 | 迁移执行 | `python -m app.db.migrate apply`，再执行 `python -m app.db.migrate status` 确认 0 个待应用且无校验和告警 |
| 5.3 | 表结构核对 | `preflight_check.py` 的"数据库表 / 数据库迁移"两项 PASS |
| 5.4 | pgvector | `002_pgvector.sql` 已执行，`vector_backend=pgvector` 生效 |
| 5.5 | 演示数据 | `AUTO_SEED=false`（生产不要自动播种演示文档与账号） |
| 5.6 | 缓存/索引重建 | 文档入库已完成 embedding；`/knowledge-bases/{code}/reindex` 按需 |

---

## 6. 上线后 30 分钟观察（以及看什么）

| # | 观察项 | 位置 | 正常表现 |
|---|---|---|---|
| 6.1 | 服务健康 | `GET /health/ready` | HTTP 200 且 `status=ready`；生产关键依赖任一失败必须返回 503 |
| 6.2 | 队列健康 | `/health/ready` → `checks.queue`，详细统计看 `/health` | `pending` 会下降；`claimed` 不超过 worker 并发总和；`dead=0` |
| 6.3 | 首个端到端任务 | 建一个真实诊断任务 | 节点时间线推进、报告有事实与引用、trace 有 token 与成本 |
| 6.4 | 成本与预算 | `GET /api/v1/costs/summary`、工作台 | 今日金额与预期量级一致；`unpriced_models` 为空 |
| 6.5 | 越权行为 | 用非授权角色试一次受限指标 | 返回 403 `metric_forbidden` / `metric_out_of_scope`，不返回数值 |
| 6.6 | 模型可用性 | trace 的 `model_used` | 为 true；若 false 看 `model_error` 与降级链 |
| 6.7 | 日志 | 服务日志 | 无 `database is locked`、无 `Checkpointer 使用进程内内存` 告警 |
| 6.8 | 运行告警 | `/health` 与系统状态页 | Webhook 启用状态与投递队列可见；`dead=0`，且页面/API 不回显 URL 或签名密钥 |
| 6.9 | Webhook 本地闭环 | `verify_webhook_integration.py` | 回环验证 HMAC 签名、503 退避重试、400 直接死信和重放；只证明本地实现 |
| 6.10 | Webhook 真实送达 | `verify_webhook_integration.py --require-production-config --allow-real-webhook-call` | 接收方授权后发送一条合成事件并写入固定真实证据文件；本地回环文件不会解除门禁 |
| 6.11 | 对象存储链路 | `verify_object_storage.py --allow-real-storage-calls --action full` | put/get/delete 全部通过，证据不含端点、密钥、对象键或正文；结果仅代表共享存储单项 |
| 6.12 | 跨副本对象可见性 | API 写入 → Worker 读取 → 容器重启后读取 → 另一 Worker 删除 | 同一探针 ID 全程可见；本地单机通过不能替代跨主机与企业存储验收 |
| 6.13 | 目标环境多节点 | `verify_target_environment.py` | 至少两个生产节点实时 `/health/ready` 全部通过，并由操作者确认确属不同主机；证据只保留 URL 指纹且 24 小时内有效 |
| 6.14 | 真实客服无指导测试 | `validate_usability_test_record.py` + 预发布报告 | 至少 5 名非研发参与者、18 项完整观察、正式结论、无未解决 P0/P1；开发自测和空模板不得通过 |
| 6.15 | 真实模型独立 holdout | `run_ticket_evaluation.py` + `real-model-holdout-result.json` | 至少 200 条独立人工标注脱敏数据，真实模型和真实事实接口至少 3 轮；业务负责人明确四项阈值，安全指标、成本定价、通过率、一致性、证据支持率和 P95 全部通过；证据不含逐条 case 详情且 168 小时内有效 |
| 6.16 | 最终人工发布批准 | `release-decision.json` + 预发布报告 | 机器门禁先全部通过；授权角色的 approve 必须绑定当前提交和证据 SHA-256，填写审批引用、风险接受并确认回滚；批准最长 24 小时，提交或证据变化后立即失效 |
| 6.17 | 交付证据制品复验 | `release-evidence` GitHub Actions job + `verify_delivery_evidence_bundle.py` | backend、frontend、e2e、production-shape 全绿后才生成；下载 `delivery-evidence-<完整提交SHA>` 后以同一 SHA 复验，报告、检查及三份远程 CI 脱敏附件的 manifest 哈希、生成批次、时效和发布提交必须全部匹配；单 runner 证据不得替代跨主机验收或 6.16 人工批准 |

---

## 7. 回滚

| # | 场景 | 动作 | 注意 |
|---|---|---|---|
| 7.1 | 配置错误（模型/限流/密级误伤） | 回滚环境变量并重启 API/worker | 最快路径；`access_policies` 改动可反向删行 |
| 7.2 | 新版本代码有问题 | 回滚镜像 tag → 重启 API 与 worker | 队列里的任务不会丢，会被新 worker 继续领取 |
| 7.3 | 数据层不可逆改动 | **先停写**（把 API 切到只读或停 worker），将发布前快照恢复到隔离库，核对版本/表/关键数据后切换连接 | 禁止直接在唯一生产库试恢复；演练入口见 5.1 |
| 7.4 | 任务被队列判死（`dead`） | 人工确认原因后重新入队（同任务同类型幂等入队） | 先看 `last_error`，别直接重放 |
| 7.5 | 模型供应商故障 | 依赖 `LLM_FALLBACK_MODELS` 自动降级；必要时切 `LLM_PROVIDER=stub` 保证可用 | stub 下报告走确定性归纳并如实标注 |

**回滚后必做**：再跑一次 `preflight_check.py`，确认配置回到预期状态（避免"改回一半"）。

---

## 8. 已知的发布限制（不要在上线后才发现）

- **CI 不会擅自调用真实模型或真实事实接口**：正式发布前必须由获授权人员按
  `docs/testing/real-model-baseline.md` 运行独立 holdout，并生成固定证据；缺失、过期或阈值失败时发布报告保持阻塞。
- **CI 不会自动批准生产发布**：全部机器门禁通过后，仍需按 `docs/testing/release-decision.md`
  完成绑定提交和证据摘要的结构化审批；手工把报告中的布尔值改成 true 不能通过下游二次校验。
- **CI 交付证据制品只证明完整性与版本绑定**：`release-evidence` job 会上传绑定 `GITHUB_SHA` 的
  报告、检查、manifest、验证摘要和三份远程单 runner 脱敏附件，但不会生成 `release-decision.json`，
  也不会解除缺失的真实外部门禁。`remote_ci_evidence_complete=true` 只表示附件齐全且未被篡改。
- **CI 的 228 条售后评测是离线 stub 规则回归**：它只证明工作流和安全门禁没有退化，
  不要拿该通过率对外承诺真实模型质量。
- **限流与缓存跨副本依赖 Redis**：没接 Redis 时它们只在单副本内生效。
- **队列没有优先级抢占**；任务队列和告警 Webhook 投递队列均已有 dead-letter 查看与管理员重放接口，
  但重放前仍必须人工核对 `last_error`，不能把自动重放当成无条件自愈。
- **接口文档在生产强制关闭**：`APP_ENV=prod` 时 `/docs`、`/redoc`、`/openapi.json` 均不可用；非生产环境可用 `EXPOSE_DOCS=false` 主动关闭。

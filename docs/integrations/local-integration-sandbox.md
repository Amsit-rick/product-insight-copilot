# 本地统一集成 Sandbox

日期：2026-11-09  
适用对象：后端开发、联调工程师、测试人员、演示验收人员

## 1. 它解决什么问题

这是一套独立的本地参考上游，用来在企业真实接口尚未提供时验证三类集成契约：

- 订单、支付、物流、售后四类只读事实接口；
- 标准化只读指标源接口；
- 带 HMAC 签名、重试和死信重放的 Webhook 接收接口。

它不连接主系统数据库和 Redis，不执行退款、赔付、改价、补发、发消息或 SQL，也不会保存
Webhook 正文。它只保留接收计数和 delivery ID。

> 通过本页所有检查，只能说明“本地/自建 Sandbox 契约已验证”。它不能证明企业生产接口、真实
> TLS/网络、权限、限流、SLA、数据质量和跨主机可达性已经验收，也不会把
> `production_ready` 改成 `true`。

## 2. 最快验证：自动启动随机端口

不需要先启动主系统，也不需要 Docker。验证脚本会在随机回环端口临时启动 Sandbox，通过真实
HTTP socket 跑完事实、指标和 Webhook 场景，然后自动停止：

```cmd
cd /d D:\path\to\product-insight-copilot\backend
..\.venv\Scripts\python.exe scripts\verify_reference_integration_sandbox.py --json-out ..\docs\testing\artifacts\reference-integration-sandbox-local.json
```

预期最后一行类似：

```text
checks=20/20 production_ready=false
```

证据仅保存场景名、通过状态、HTTP 状态码和标准错误码，不保存地址、API Key、Webhook Secret、
订单事实值、指标值或请求正文。

## 3. CMD 手动启动

终端 A：

```cmd
cd /d D:\path\to\product-insight-copilot\backend
set SANDBOX_API_KEY=local-sandbox-key
set SANDBOX_WEBHOOK_SECRET=local-webhook-secret
..\.venv\Scripts\python.exe scripts\reference_integration_api.py --port 8091
```

访问：

- 健康检查：`http://127.0.0.1:8091/health`
- Swagger：`http://127.0.0.1:8091/docs`

终端 B：

```cmd
cd /d D:\path\to\product-insight-copilot\backend
set SANDBOX_API_KEY=local-sandbox-key
set SANDBOX_WEBHOOK_SECRET=local-webhook-secret
..\.venv\Scripts\python.exe scripts\verify_reference_integration_sandbox.py --base-url http://127.0.0.1:8091 --json-out ..\docs\testing\artifacts\reference-integration-sandbox-running.json
```

这些默认密钥只允许本机演示。局域网或共享环境必须自行设置随机密钥，并限制监听地址和网络访问。

## 4. Docker Compose 可选 profile

Sandbox 不属于默认全栈服务，只有显式启用 `sandbox` profile 才会启动：

```cmd
cd /d D:\path\to\product-insight-copilot
set SANDBOX_API_KEY=replace-with-random-local-key
set SANDBOX_WEBHOOK_SECRET=replace-with-random-local-secret
set SANDBOX_PORT=8091
docker compose -f deploy\docker-compose.yml --profile sandbox up -d --build integration-sandbox
docker compose -f deploy\docker-compose.yml --profile sandbox ps integration-sandbox
```

停止：

```cmd
docker compose -f deploy\docker-compose.yml --profile sandbox stop integration-sandbox
docker compose -f deploy\docker-compose.yml --profile sandbox rm -f integration-sandbox
```

该服务没有 `depends_on`，不会启动 Postgres、Redis、API 或 Worker。

## 5. 接口清单

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/health` | 健康检查，无鉴权 |
| `GET` | `/orders/{order_id}` | 订单事实 |
| `GET` | `/payments/{order_id}` | 支付事实 |
| `GET` | `/logistics/{order_id}` | 物流事实 |
| `GET` | `/aftersales/{order_id}` | 售后事实 |
| `GET` | `/api/v1/metric-snapshots` | 标准化指标快照 |
| `POST` | `/webhooks/alerts` | 正常 Webhook 接收 |
| `POST` | `/webhooks/alerts/retry` | 首次 503、再次成功 |
| `POST` | `/webhooks/alerts/dead` | 首次 400、显式重放成功 |
| `GET` | `/webhooks/alerts/stats` | 仅返回脱敏计数元数据 |

除 `/health` 和三个 Webhook 接收路径外，其余接口使用 `X-Api-Key`。Webhook 签名算法为：

```text
sha256(secret, timestamp + "." + raw_body)
```

请求头为 `X-Alert-Timestamp`、`X-Alert-Delivery-Id` 和 `X-Alert-Signature`。

## 6. 固定故障场景

事实接口：

| 标识 | 行为 |
|---|---|
| `SANDBOX-OK-001` | 成功并返回标准结构 |
| `SANDBOX-NOTFOUND-001` | 404 |
| `SANDBOX-AUTH-001` | 401 |
| `SANDBOX-BADJSON-001` | 200 但正文不是 JSON |
| `SANDBOX-TIMEOUT-001` | 物流接口延迟，用于客户端超时验证 |

指标接口：

| 租户或参数 | 行为 |
|---|---|
| 普通隔离租户 + 白名单指标 | 返回标准快照 |
| 未知指标 | 422 |
| `SANDBOX-CROSS-TENANT` | 返回其他租户行，验证适配器整批拒绝 |
| `SANDBOX-METRIC-RATE-LIMIT` | 429 + `Retry-After` |
| `SANDBOX-METRIC-ERROR` | 503 |

白名单指标固定为 `crash_rate`、`next_day_retention` 和 `conversion_rate`。

## 7. 接入主系统时的对应关系

事实适配器配置：

```cmd
set TICKET_USE_MOCK_ADAPTERS=false
set TICKET_FACT_ORDER_URL=http://127.0.0.1:8091/orders/{order_id}
set TICKET_FACT_PAYMENT_URL=http://127.0.0.1:8091/payments/{order_id}
set TICKET_FACT_LOGISTICS_URL=http://127.0.0.1:8091/logistics/{order_id}
set TICKET_FACT_AFTERSALE_URL=http://127.0.0.1:8091/aftersales/{order_id}
set TICKET_FACT_AUTH_HEADERS={"X-Api-Key":"local-sandbox-key"}
```

指标源配置：

```cmd
set METRIC_SOURCE_HTTP_ENABLED=true
set METRIC_SOURCE_HTTP_URL=http://127.0.0.1:8091/api/v1/metric-snapshots
set METRIC_SOURCE_HTTP_AUTH_HEADERS={"X-Api-Key":"local-sandbox-key"}
```

Webhook 接收地址用于验证主系统投递器时，可分别指向正常、重试或死信路径。密钥必须与
`SANDBOX_WEBHOOK_SECRET` 一致。

生产环境仍必须改为企业 HTTPS 地址、最小权限凭据和隔离测试租户，并分别执行真实事实接口、真实
指标源和真实外部 Webhook 的发布门禁脚本。

## 8. 主系统隔离联调模式

只启动 Sandbox 只能证明接口自身可用。要验证主系统确实调用它，并同时演练反思、队列和断点恢复，使用隔离联调启动器：

```cmd
cd /d D:\path\to\product-insight-copilot
.venv\Scripts\python.exe backend\scripts\local_integration.py run
```

默认地址：

- 主系统：`http://127.0.0.1:8033/#/login`
- Sandbox 文档：`http://127.0.0.1:8091/docs`

此模式有意与正常开发环境隔离：

- 使用 `backend/.tmp/local-integration/integration.db`，不写 `data/pic.db`；
- 使用独立任务 worker、持久队列和 SQLite checkpointer；
- 开启工单反思及自动安全门禁；
- 事实与指标走真实 HTTP，但返回值仍是合成数据；
- 沿用 `backend/.env` 的模型配置，不复制或打印模型密钥；
- `Ctrl+C` 仅停止本次启动器创建的子进程，联调数据库和检查点保留。

真实模型调用必须显式授权，验收脚本才会运行：

```cmd
.venv\Scripts\python.exe backend\scripts\verify_local_integration_workflow.py --allow-real-models
```

该脚本创建带 `demo` / `test` 标记的隔离工单，不发送客户消息、不执行退款，也不访问企业接口。它验证成功、404、鉴权失败、无效 JSON、物流超时降级、反思、主管审批和指标同步，并把脱敏结果写入 `.tmp`。

跨进程恢复使用零成本的 stub 模型单独验证：

```cmd
.venv\Scripts\python.exe backend\scripts\verify_local_integration_recovery.py
```

该演练证明同一台机器上的 SQLite 队列和检查点可跨 Python 进程恢复；它不等价于跨主机或托管 PostgreSQL 故障切换。

## 9. 本地真实语义检索模式

真实 Embedding / Reranker 是可选的独立进程，不进入主 API 的默认依赖。首次使用先安装轻量运行依赖并下载模型：

```cmd
cd /d D:\path\to\product-insight-copilot
.venv\Scripts\pip.exe install -r backend\requirements-local-models.txt
.venv\Scripts\python.exe backend\scripts\local_integration.py run --with-local-models
```

此模式固定使用：

- `BAAI/bge-small-zh-v1.5`，512 维 Embedding；
- `BAAI/bge-reranker-v2-m3-int8`，CPU ONNX Reranker；
- 本地模型服务使用 4 个模型线程和共享串行推理通道；健康检查返回
  `model_threads` 与 `inference_policy`，便于把压测结果对应到运行策略；
- 本机并发实测表明 Reranker 吞吐约 3 QPS，并发增加主要放大长尾；该服务用于本地开发与评测，
  不作为生产模型服务。详细证据见 `docs/testing/本地真实模型并发P3-17-2026-11-09.tdd.md`；
- `EMBEDDING_FAILURE_MODE=error` 与 `RERANKER_FAILURE_MODE=error`，验收时禁止静默回退；
- `backend/.tmp/local-integration/semantic` 独立数据库和检查点，不与旧 1024 维 stub 索引混用；
- 模型接口 Bearer 鉴权，健康页只显示模型身份和接入状态，不回显端点或密钥。

模型服务默认监听 `8093`，完整启动顺序为模型服务、Sandbox、主 API、Worker。首次下载约需 0.66GB，模型缓存位于
`backend/.tmp/fastembed-cache`，不提交 Git。

只验证语义检索时运行：

```cmd
.venv\Scripts\python.exe backend\scripts\verify_local_semantic_retrieval.py
```

验收器检查模型身份、512 维输出、相关文档重排、16 条中文政策查询、Recall@5、MRR、重排覆盖率、延迟和
Embedding 回退计数。当前 CPU 实测查询 p95 约 2.44 秒，适合本地验收，不代表生产延迟目标；生产仍需独立业务
文档 holdout、目标硬件压测和外部向量库验证。

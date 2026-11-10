# Product Insight Copilot

Product Insight Copilot 是一个面向电商售后与产品诊断场景的受控多 Agent 决策平台。系统把订单、支付、物流、售后事实与企业知识库结合，生成可追溯的处理建议，并通过风险分级、人工审批和服务端门禁约束高风险动作。

当前可直接运行的主线是“售后工单智能处理”：客服提交工单后，系统补齐业务事实、检索适用政策、判断风险、生成回复草稿，并在退款等资金动作执行前要求相应级别的审核。

## 核心能力

- **受控多 Agent 工作流**：产品知识、用户反馈、版本记录、指标分析等 Agent 分工执行，由工作流负责任务路由、合并、重试、恢复与终止。
- **混合知识检索**：支持文档解析、切分、向量召回、关键词召回、重排和来源引用。
- **只读事实查询**：订单、支付、物流和售后适配器默认只读；模型不能直接修改业务数据。
- **风险与审批治理**：动作、金额和风险由确定性规则判定；高风险结论进入人工审核。
- **反思与证据校验**：独立校验结论的证据覆盖、时间一致性与冲突，必要时触发有界补检索。
- **全链路追踪**：记录任务、Agent 节点、模型调用、检索、工具调用、引用、审批和报告。
- **可恢复执行**：任务状态和检查点可持久化，支持失败重试、暂停后继续以及任务回放。
- **本地零依赖演示**：可使用 SQLite、内存缓存、本地文件存储和 stub 模型完整体验流程。

## 处理链路

```text
工单/问题输入
  -> 参数与权限校验
  -> 意图识别和受限计划
  -> 事实查询 + 知识检索 + 专业 Agent 并行分析
  -> 证据合并与冲突检查
  -> 反思判定（通过 / 补检索 / 人工审核 / 拒绝）
  -> 生成处理建议或诊断报告
  -> 服务端执行门禁
  -> 审计、追踪与回放
```

模型负责抽取、归纳和文本生成，不负责最终风险判定，也没有资金动作的直接执行权。

## 技术栈

- 后端：Python 3.12、FastAPI、Pydantic、SQLAlchemy、LangGraph
- 前端：Vue 3、TypeScript、Vite、Pinia、Element Plus
- 数据：PostgreSQL / SQLite、pgvector / fallback vector store、Redis / memory cache
- 存储：MinIO / S3-compatible storage / local storage
- 模型：DeepSeek、Qwen 等 OpenAI-compatible API，支持离线 stub
- 交付：Docker、Docker Compose、GitHub Actions

## 快速启动

### Docker 演示模式

前提：已安装并启动 Docker Desktop。

```bash
docker compose -f deploy/docker-compose.demo.yml up -d --build
```

浏览器打开 `http://127.0.0.1:8010/`。

| 角色 | 用户名 | 密码 |
|---|---|---|
| 运营 | `ops` | `ops123` |
| 审核 | `reviewer` | `reviewer123` |
| 管理员 | `admin` | `admin123` |

```bash
docker compose -f deploy/docker-compose.demo.yml ps
docker compose -f deploy/docker-compose.demo.yml logs -f demo
docker compose -f deploy/docker-compose.demo.yml down
```

演示模式使用公开测试账号、SQLite 和 stub 模型，仅用于本地体验，不可直接暴露到公网。

### 本地开发

后端（Windows CMD）：

```bat
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev-windows.lock
copy .env.example .env
set PYTHONPATH=%CD%
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

前端：

```bat
cd frontend
npm ci
npm run dev
```

默认配置不需要真实模型密钥。接入真实模型、PostgreSQL、Redis、对象存储或企业事实接口时，请根据 `backend/.env.example` 和 `deploy/.env.example` 配置环境变量，禁止提交 `.env`、Token、私钥或生产数据。

## 测试

后端：

```bat
cd backend
set PYTHONPATH=%CD%
.venv\Scripts\python -m pytest tests/unit -q
```

前端：

```bat
cd frontend
npm run typecheck
npm run build
```

更多验收步骤见 `功能使用说明书.md`、`界面测试操作手册.md`、`RELEASE_CHECKLIST.md` 和 `docs/testing/`。

## 目录结构

```text
backend/       FastAPI、工作流、Agent、RAG、治理与测试
frontend/      Vue 3 管理界面
deploy/        Docker Compose 与发布配置
docs/          架构、集成、测试和产品改造文档
scripts/       构建、验收和供应链脚本
.github/       GitHub Actions
```

## 安全边界

- 模型生成内容不是可直接执行的业务指令。
- 业务事实只能通过授权的只读适配器获取。
- DDL、DML、任意 SQL 和未授权字段访问不对模型开放。
- 高风险动作必须经过人工审批，接口层会拒绝未批准的执行请求。
- 引用缺失、事实冲突或证据不足时，系统应降级、补检索或转人工，不伪造确定结论。
- 日志和追踪信息需要脱敏；生产密钥必须通过部署环境注入。

## 当前交付边界

仓库包含可运行的本地演示、前后端代码、自动化测试和部署模板。生产上线仍需由目标环境完成：

- 真实模型、Embedding、Reranker 的质量与成本评测
- 真实订单、支付、物流和售后只读接口联调
- PostgreSQL、Redis、对象存储和向量库容量验证
- 企业级身份、租户权限、审计保留期和密钥管理
- 压测、故障恢复、监控告警、备份恢复与安全扫描

这些外部依赖未完成验收前，不应把本地演示结果表述为生产可用。

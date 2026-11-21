# Saleor 高保真业务 Sandbox 开发日志

## 2026-11-15：Slice 0–2

### 本轮完成

- 完成 Saleor 3.23.49 兼容性 Spike，三次独立空环境迁移、播种、启动和查询成功。
- 新增默认关闭的 `docker-compose.commerce-sandbox.yml`，不改变现有默认 Compose。
- 将数据库、缓存、媒体、播种标记和运行时凭据分离到专用卷；Saleor 仅连接内部网络。
- 播种管理员与运行时 App 分离；主 Sandbox 只挂载 App Token 文件。
- 新增 `FactProvider`、`StaticFactProvider` 和 `SaleorFactProvider`，保持原有四类 HTTP GET 契约。
- 固定 Saleor Query，不接受外部 GraphQL 文本，不包含 Mutation、Introspection 或客户 PII 字段。
- 实现超时、有限重试、响应体大小上限、结构校验、固定字段映射和错误语义。
- 高保真上游失败时返回结构化错误，不静默回退静态成功数据。

### 现场发现并修复

1. Saleor 3.23.49 `populatedb` 不支持 `--orders`；改用官方默认 20 订单。
2. 非 root bootstrap 无法写新建 Docker 卷；增加一次性目录权限初始化容器，长驻容器仍为非 root。
3. `populatedb --createsuperuser` 会在日志打印密码字段；改为 `populatedb` 后执行
   `createsuperuser --noinput`，实测密码未进入日志。
4. 只使用 Docker internal network 时宿主端口未真正发布；保持 Saleor 内部隔离，仅给标准化
   FastAPI Sandbox 增加独立 edge network。
5. 重复 `start` 会重跑 one-shot 服务；增加专用播种标记卷，订单数实测保持 20 → 20。

### 验证

- 针对性 pytest：25 通过、0 失败。
- 完整空卷启动：迁移、播种、API、App 身份、Sandbox 全部成功。
- `GET /health`：`fact_provider=saleor`、`fact_provider_status=saleor_ready`。
- 订单、支付、物流、售后四类 GET 实测成功，`source_version=saleor-3.23.49`。
- 播种密码和 App Token 日志匹配均为 false。
- 机器可读证据：`docs/testing/artifacts/saleor-commerce-sandbox-slice1-2-local.json`。

### 进度与下一步

- Slice 0：100% （权重 10%）。
- Slice 1：100% （权重 20%）。
- Slice 2：100% （权重 25%）。
- Saleor 专项总进度：**55%**。
- 下一步：Slice 3，将官方随机示例数据收口为十类可命名、可复现、可独立重置的业务场景。

证据边界不变：本轮使用本地 Saleor 合成数据，未连接企业 OMS、真实支付、快递或
售后系统，`production_ready=false`。

## 2026-11-15：Slice 3

### 本轮完成

- 新增确定性场景清单生成器，从 Saleor 本地订单摘要中选择十个不同订单，不复用订单凑场景。
- 新增十类场景别名：正常签收、未支付取消、已支付未发货退款、部分发货、物流停滞、
  已签收退货、部分退款、重复退款、少件/漏发和跨来源冲突。
- 新增 `ScenarioFactProvider`：场景别名先查询真实 Saleor 订单，再合并显式 local overlay；
  普通数字订单继续直通 Saleor，不应用覆盖层。
- 覆盖层仅允许四类事实契约中的安全字段，额外禁止地址、手机号、邮箱、客户号和凭据字段；
  未知字段或敏感字段会导致清单加载失败。
- 新增一次性 `commerce-scenarios` 容器和专用 `commerce-scenario-manifest` 卷；生成容器读写，
  长驻事实服务只读挂载。
- 新增鉴权只读接口 `GET /scenarios`，仅展示场景名称、用途、来源类型和是否使用覆盖层，
  不展示上游订单映射、覆盖内容、GraphQL 或凭据。
- 新增 `verify_saleor_scenarios.py`，通过真实 HTTP 自动检查十个场景的四类事实契约。

### TDD 与回归

- RED：新增测试最初因缺少 `ScenarioFactProvider` 和场景模块在收集阶段失败；Compose 交付测试
  因缺少场景容器和生成脚本失败。
- GREEN：Slice 1–3 相关回归 38 通过、0 失败。
- 覆盖率：`sandbox_services.scenarios` 86%，`app + providers + scenarios` 合计 85%。
- Docker Compose 配置校验通过，重建后 `saleor_ready`、`scenario_manifest_status=ready`。
- 容器级自动验收 43/43 通过；十场景四来源 40/40 查询成功，P50 61.30ms、P95 87.59ms。
- 第二次启动日志为 `Scenario manifest already exists: 10 scenarios`，场景清单生成幂等。

### 证据与边界

- 机器证据：`docs/testing/artifacts/saleor-commerce-sandbox-slice3-local.json`。
- 场景事实由“Saleor 本地合成订单 + 明确标注的本地覆盖层”构成；覆盖层不是 Saleor 原生事实，
  更不是企业真实接口数据。
- `enterprise_endpoint_verified=false`、`real_payment_verified=false`、
  `real_carrier_verified=false`、`production_ready=false` 保持不变。

### 进度与下一步

- Slice 0：100%（权重 10%）。
- Slice 1：100%（权重 20%）。
- Slice 2：100%（权重 25%）。
- Slice 3：100%（权重 20%）。
- Saleor 专项总进度：**75%**。
- 下一步：Slice 4，接入主系统 `HttpFactAdapter`，自动化验证十类业务场景、Saleor 上游不可达、
  查询超时、字段缺失和跨来源冲突下的补证据、有限修订与人工审核行为。

## 2026-11-15：Slice 4

### 本轮完成

- 主系统 `HttpFactAdapter` 增加订单、支付、物流、售后关键字段门禁，空数据或缺关键字段不再
  被误判为成功事实。
- `source_version` 从 Sandbox 响应贯穿适配器、事实表、接口、展示与 Task Trace；新增
  PostgreSQL 迁移及 SQLite 存量补列。
- 新增跨来源冲突检测：订单显示已支付/履约但支付金额为 0 时转人工核验。
- 退款金额优先引用 `refundable_amount`；剩余可退余额为 0 时阻止重复退款。
- 任一事实来源异常时，退款诉求不再生成退款承诺，保留其他三个来源事实并转人工核验。
- 新增六类故障：物流超时、支付字段缺失、非法观测时间、支付限流、Saleor 上游 5xx、
  售后非法 JSON；公开 `GET /fault-scenarios` 仅返回安全元数据。
- 新增主系统 E2E 验收器 `verify_saleor_main_workflow.py`，使用临时隔离数据库、Stub 模型和
  Inline 执行，真实 HTTP 访问 8092 Sandbox。
- 修复正常签收被单独关键词“签收”误判为物流停滞的问题；新增“退还剩余可退金额”退款意图。
- 修复 SQLite checkpointer 重置时连接未关闭导致 Windows 临时目录无法删除的问题。

### TDD 与回归

- RED：Task Trace 缺少 `fact_sources`；任一非支付来源失败仍会生成退款；验收器尚不存在；
  正常签收被误判为物流停滞；“退还剩余可退金额”未识别为退款。
- GREEN：相关单元/集成测试 155 通过、0 失败。
- 覆盖率：`app.adapters.http` 87%、`sandbox_services.app` 93%、
  `app.agents.after_sales` 76%，三个核心模块合计 81%。
- 主系统 E2E：十类业务场景 10/10、六类技术故障 6/6。

### 证据与边界

- 机器证据：`docs/testing/artifacts/saleor-commerce-sandbox-slice4-local.json`。
- 证据不保存事实值、上游订单映射、Token、GraphQL 正文、授权头或客户 PII。
- `environment=local_high_fidelity_sandbox`。
- `enterprise_endpoint_verified=false`、`real_payment_verified=false`、
  `real_carrier_verified=false`、`production_ready=false`。

### 进度与下一步

- Slice 0–4：100%。
- Saleor 专项总进度：**90%**。
- 下一步：Slice 5，补充延迟/失败类型/人工接管指标，完善新环境启动演示、架构说明、交付状态
  与 README/交付证据边界复核。

## 2026-11-16：Slice 5 本地工程收口

### 本轮完成

- `verify_saleor_main_workflow.py` 增加总/业务/故障成功率、主流程 P50/P95、事实查询 Agent
  P50/P95、失败类型计数和人工接管原因计数。
- 每个场景只写入延迟、错误码、动作和安全枚举；仍禁止保存事实 payload、上游订单映射、
  客户信息、Token、授权头和 GraphQL 正文。
- README、ADR、Sandbox 启动手册、3 分钟演示脚本和交付状态统一为三层证据口径：
  Static Sandbox、Saleor High-Fidelity Sandbox、Enterprise Endpoint。
- 修正演示稿“为什么它敢上生产”的过度表述为“为什么它不会乱执行”，明确企业真实端点、
  真实模型盲测和真人客服验收仍未完成。

### TDD 与回归

- RED：新增测试因缺少 `_percentile`、`summarize_results` 和场景延迟字段失败，3 项失败符合预期。
- GREEN：验收器单测 11/11；Saleor 相关单元/集成回归 158/158。
- 验收器单测与真实 E2E 合并覆盖率：95%（263 行语句，12 行未覆盖）。
- 主系统 E2E：业务场景 10/10、技术故障 6/6，总成功率 100%。
- 本机样本：主流程 P50/P95 338/947 ms；事实查询 Agent P50/P95 107/138 ms。
- 机器证据：`docs/testing/artifacts/saleor-commerce-sandbox-slice5-local.json`。

### 证据边界与剩余项

- `environment=local_high_fidelity_sandbox`。
- `enterprise_endpoint_verified=false`、`real_payment_verified=false`、
  `real_carrier_verified=false`、`production_ready=false`。
- Slice 5 本地代码、自动化和文档已经完成；仍需一名未参与开发的人按文档执行一次冷启动并
  留下真实演示记录。开发者不能用自己的自动化结果替代这项独立验收。
- 新增默认失败的独立验收模板和 `validate_saleor_cold_start_record.py`：验证独立声明、全新环境、
  执行步骤、健康状态、Slice 5 原始文件 SHA-256 和五项真实性边界；验证结果不保存人员身份。
- CI 新增可选 `docker-compose.commerce-sandbox.yml` 静态解析，使用仅供插值的占位值，不启动
  Saleor，也不依赖开发机 `.commerce-sandbox.env`。
- 冷启动验证器测试 6/6，覆盖率 93%；扩展后的 Saleor 相关回归 187 项通过。

### 进度

- Slice 0–4：100%。
- Slice 5 本地工程项：100%；独立冷启动验收：未完成。
- Saleor 专项量化进度：**99%**。

## 2026-11-16：提交前最终开发者验收

### 修复与根因

- 全量后端测试发现全新安装入口 `001_init.sql` 缺少 `order_facts.source_version`；增量迁移
  `016_order_fact_source_version.sql` 已有该字段，因此升级库正常、新建库结构不完整。已同步初始
  迁移，保证两条安装路径一致。
- 前端构建与后端全量测试并行时，`vite build` 会短暂清空 `frontend/dist`，导致根路径静态页面
  测试瞬时返回 404。前端构建完成后串行复跑通过，确认是验收执行竞态，不是产品路由缺陷。

### 最终验证

- 后端全量测试：全部通过，1 项既有 skip，无失败。
- 前端 `typecheck` 与生产构建：通过；仅保留不影响构建的第三方 Rolldown 注释警告。
- 默认、Release、Saleor Commerce 三套 Compose 配置静态解析：通过。
- Saleor Sandbox 健康：事实 Provider 与场景清单均为 ready。
- Saleor 主链路：业务 10/10、故障 6/6，总成功率 100%。
- 最新本机样本：主流程 P50/P95 538/1000 ms；事实查询 Agent P50/P95 167/239 ms。

### 交付边界

本轮是开发者提交前验收，不能替代未参与开发人员的独立冷启动记录。环境仍为
`local_high_fidelity_sandbox`；企业真实 OMS、支付、物流与生产网络均未验证，
`enterprise_endpoint_verified=false`、`real_payment_verified=false`、
`real_carrier_verified=false`、`production_ready=false`。

### 进度

- 本地工程开发、自动化测试和交付文档：100%。
- 独立冷启动验收：未完成。
- Saleor 专项量化进度：**99%**。

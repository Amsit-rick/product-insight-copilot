# ADR-003：采用 Saleor 作为可选高保真电商 Sandbox

日期：2026-11-15
状态：Accepted for local sandbox only
证据等级：本地隔离环境实测
生产准备：`production_ready=false`

## 决策

在现有 FastAPI 集成 Sandbox 之后增加可选 `SaleorFactProvider`。Saleor 只提供订单、
交易、履约和退款状态，不进入 Agent/LangGraph 核心，不替换现有四类 HTTP GET
契约。默认仍使用静态 Provider，只在显式启用本地高保真环境时访问 Saleor。

## 冻结版本

| 项目 | 冻结值 |
|---|---|
| Saleor Platform 模板提交 | `ab6315bd59c58b4815175df4c679107ff9695be4` |
| Saleor API 镜像 | `ghcr.io/saleor/saleor:3.23.49` |
| Saleor API digest | `sha256:bb59cbf1efd764d9010390a4f08d681f17f771528b5e0688c641158e9c16680b` |
| PostgreSQL 镜像 | `postgres:15-alpine` |
| PostgreSQL digest | `sha256:fc41cfa8a90c09e84dd723ee764be4b2f59ba6c98b1e5b3fca540adddf68a5ba` |
| 缓存 | `valkey/valkey:8.1-alpine` |
| 上游许可证 | BSD-3-Clause |
| 本机环境 | Windows + Docker Desktop 4.88.0 + Engine 29.7.2 + Linux amd64 |

## 重复启动证据

使用三个独立 Compose project、独立网络和独立数据卷，从空数据库执行迁移、
`populatedb --createsuperuser`、生成 20 个订单并启动 API。没有手工修改容器文件或
数据库。

| 次数 | Compose project | 端口 | 结果 |
|---:|---|---:|---|
| 1 | `pic-saleor-spike` | 18000 | 迁移、播种、查询和 API 重启后查询成功 |
| 2 | `pic-saleor-spike-b` | 18001 | 独立空环境完整成功 |
| 3 | `pic-saleor-spike-c` | 18002 | 独立空环境完整成功 |

第三次验证返回 20 个订单、20 条交易和 11 个履约记录，覆盖
`UNFULFILLED`、`PARTIALLY_FULFILLED`、`FULFILLED` 和 `UNCONFIRMED`。

## 字段可用性结论

已验证可通过受权 GraphQL Query 读取：

- Order：`number`、`status`、`total`、`totalCharged`、`totalRefunded`、`lines`；
- TransactionItem：`authorizedAmount`、`chargedAmount`、`refundedAmount`、`events`；
- Fulfillment：`status`、`trackingNumber`、`lines`、`reason`；
- 退款：可由 `totalRefunded`、Transaction 退款金额及事件读取。

详细物流轨迹、承运商、停滞时长、营销活动核算和企业售后工单不是 Saleor
的完整事实，必须由本地 overlay 或真实企业上游补充；不允许由模型猜测。

## 资源占用

稳定运行时实测约：Saleor API 503 MiB、PostgreSQL 72 MiB、Valkey 5.5 MiB，
合计约 580 MiB。因此必须保持为默认关闭的可选 profile，不能进入快速单元测试。

## 只读风险与补偿控制

Saleor 的 `MANAGE_ORDERS` 不是严格的“只读权限”。因此运行时身份与播种管理员
必须分离，并且同时实施：

1. Provider 内部只保留编译期固定 Query，不接收外部 GraphQL 文本；
2. 显式拒绝 Mutation 与运行时 Introspection；
3. 主系统只访问 Sandbox 的四个 GET 契约；
4. Saleor 网络与主应用隔离，只向 Sandbox 开放；
5. 播种凭据不进入主应用容器，运行时 Token 不进入日志和测试制品。

本地 Spike 使用的示例管理员和临时 RSA/JWT 材料仅存在于隔离临时环境，
不得复用到正式本地 profile 或生产。

## 未被证明的事项

- 未验证企业 OMS、真实支付网关、快递商或售后平台；
- 未完成企业身份、秘钥托管、跨主机部署或生产 SLA 验证；
- 本 ADR 和后续本地验收不解除任何生产门禁。

## 完成后的证据分层

| 层级 | 数据来源 | 可以证明 | 不能证明 |
|---|---|---|---|
| Static Sandbox | 固定合成 JSON | 四类 HTTP 契约、错误语义和快速回归 | 连续业务状态、企业接口可用性 |
| Saleor High-Fidelity Sandbox | Saleor 本地合成订单 + 显式 local overlay | 状态驱动场景、跨来源一致性、主系统只读 HTTP 链路 | 真实支付清算、真实快递轨迹、企业 SLA |
| Enterprise Endpoint | 企业隔离测试租户 | 企业协议、鉴权、网络、数据口径和故障恢复 | 当前尚无证据 |

2026-11-16 主系统验收使用 Stub 模型、Inline 执行和真实本地 HTTP，共完成业务场景 10/10、
故障场景 6/6；主流程 P50/P95 为 338/947 ms，事实查询 Agent P50/P95 为 107/138 ms。
机器证据为 `docs/testing/artifacts/saleor-commerce-sandbox-slice5-local.json`，并固定保存
`environment=local_high_fidelity_sandbox`、`enterprise_endpoint_verified=false`、
`real_payment_verified=false`、`real_carrier_verified=false`、`production_ready=false`。

# 售后事实 HTTP 接入契约

本契约是系统核心与真实 OMS、支付、物流、售后平台之间的只读边界。供应商原始字段先在企业 API
网关或独立映射层转换为下述标准结构，核心业务不直接猜测不同厂商的私有字段。

## 1. 请求

四个端点均使用 `GET`，配置的 URL 必须包含 `{order_id}`：

```text
TICKET_FACT_ORDER_URL=https://gateway.example.com/orders/{order_id}
TICKET_FACT_PAYMENT_URL=https://gateway.example.com/payments/{order_id}
TICKET_FACT_LOGISTICS_URL=https://gateway.example.com/logistics/{order_id}
TICKET_FACT_AFTERSALE_URL=https://gateway.example.com/aftersales/{order_id}
```

- `order_id` 按 URL path segment 编码后替换。
- 系统发送 `X-Request-ID`；上游应原样回传，便于跨系统追踪。
- 售后端点在有客户号时附加 `customer_id` 查询参数。
- 鉴权头只从 `TICKET_FACT_AUTH_HEADERS` 注入，不写入日志、健康检查或前端。
- 生产环境四个端点必须使用 HTTPS；URL 禁止内嵌用户名、口令或片段，鉴权只能走服务端 Header。
- 适配器不会调用退款、赔付、改价、补发等写接口。

## 2. 成功响应

```json
{
  "data": {
    "order_status": "已签收",
    "order_amount": "268.00",
    "order_items": "SKU-100 x 1",
    "order_channel": "自营商城"
  },
  "observed_at": "2026-09-25T12:00:00+08:00",
  "request_id": "oms-request-10001"
}
```

`data` 必须是 JSON 对象。`observed_at` 缺省时使用本系统接收时间；存在时必须为 ISO-8601。
响应中的未知字段会被丢弃，手机号和地址在适配器出口脱敏。

允许字段：

| 来源 | 字段 |
|---|---|
| 订单 | `order_status`、`order_amount`、`order_items`、`order_channel`、`address`、`receiver_phone`、`category`、`special_category` |
| 支付 | `paid_amount`、`refundable_amount`、`pay_channel` |
| 物流 | `carrier`、`tracking_no`、`latest_track`、`signed_at`、`stalled_hours`、`eta`、`package_weight` |
| 售后 | `aftersale_history`、`last_case_result`、`gross_amount`、`eligible_amount`、`excluded_items`、`promotion_threshold`、`promotion_result`、`customer_id` |

金额使用字符串，系统只引用上游事实，不用模型改写或推算最终财务金额。

## 3. 故障语义

| 上游情况 | 本系统错误码 | 是否重试 |
|---|---|---|
| 404 | `order_not_found` | 否 |
| 401 / 403 | `adapter_auth_error` | 否 |
| 408 / 客户端超时 | `adapter_timeout` | 是 |
| 429 | `adapter_rate_limited` | 是 |
| 5xx / 网络错误 | `adapter_upstream_error` / `adapter_unavailable` | 是 |
| 非 2xx 的其他 4xx | `adapter_request_rejected` | 否 |
| 非 JSON、`data` 非对象、时间非法 | `adapter_invalid_response` | 否 |

重试次数和指数退避由 `TICKET_FACT_MAX_RETRIES`、`TICKET_FACT_RETRY_BACKOFF_SECONDS`
控制。四个来源并行查询；单个来源失败会以结构化事实落库，不会把接口正文或鉴权信息带入错误消息。
`order_not_found` 的错误文本不会复制原始订单号，避免订单标识进入数据库、审计日志或页面。

## 4. 上线门禁

```cmd
cd backend
..\.venv\Scripts\python.exe scripts\preflight_check.py --strict
```

生产环境必须设置 `TICKET_USE_MOCK_ADAPTERS=false`。四个端点缺失、没有 `{order_id}`、不是 HTTPS、
URL 内嵌凭据/片段或地址格式非法时，
preflight 直接失败。`/health` 的 `fact_adapter` 只展示后端类型、配置完整性、超时和重试次数，
不展示 URL 与鉴权头。

## 5. 真实测试环境契约验收

静态 preflight 只能证明配置完整，不能证明目标系统真的可达、字段映射正确或故障语义符合契约。
联网前先校验场景计划；示例计划只引用环境变量名，不保存真实订单号或客户号：

```cmd
.venv\Scripts\python.exe backend\scripts\verify_fact_integrations.py --validate-plan-only
```

在厂商 sandbox/API 网关中准备匿名测试数据后，通过当前 CMD 会话注入标识。不要把这些值写入
计划、Git 或 JSON 结果：

```cmd
set FACT_PROBE_SUCCESS_ORDER_ID=<sandbox-success-order-id>
set FACT_PROBE_SUCCESS_CUSTOMER_ID=<sandbox-anonymous-customer-id>
set FACT_PROBE_NOT_FOUND_ORDER_ID=<sandbox-not-found-order-id>
set FACT_PROBE_LOGISTICS_TIMEOUT_ORDER_ID=<sandbox-logistics-timeout-order-id>
.venv\Scripts\python.exe backend\scripts\verify_fact_integrations.py --allow-real-fact-adapters --runs 3 --json-out docs\testing\artifacts\fact-integration-result.json
```

探测器会并行调用四个只读 `GET` 端点，验证预期结果、字段白名单、必填映射、非 mock、请求号、
观测时间新鲜度、敏感值和 P95。证据文件仅保存场景 ID、字段名、错误码与延迟，不保存 URL、
鉴权头、订单号、客户号、响应值或上游错误正文。

`logistics-timeout` 场景要求 sandbox 或 API 网关支持按专用订单号注入物流超时。若目标环境暂不支持，
应从自己的计划中删除该场景并把“外部故障演练”保留为未完成，不能用本地 mock 的超时结果替代。

## 6. 本地回环演练（厂商 sandbox 之前）

`backend/scripts/reference_fact_api.py` 提供一个只读参考服务，覆盖成功、404、401、物流超时、
非 JSON 五类场景；`scripts/verify_fact_local_rehearsal.py` 会用真实 HTTP 适配器跑 5 个场景、
181 项检查并输出 `docs/testing/artifacts/fact-integration-local-check.json`。

```cmd
cd backend
..\.venv\Scripts\python.exe scripts\verify_fact_local_rehearsal.py
```

场景计划：`docs/integrations/fact-probe-plan.local.json`。完整接入清单见
[fact-sandbox-onboarding.md](fact-sandbox-onboarding.md)。

这一步只证明适配器实现正确，**不能**替代厂商 sandbox 证据（§5 的 3 轮真实探针）。

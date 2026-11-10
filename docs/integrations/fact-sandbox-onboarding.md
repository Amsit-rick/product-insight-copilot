# 售后事实接口沙箱接入包

日期：2026-11-08
适用对象：平台方 OMS/支付/物流/售后接口负责人、项目对接工程师
上游契约：[fact-http-contract.md](fact-http-contract.md)

## 1. 这个包解决什么

在拿到厂商 sandbox 之前，用**本地回环参考服务**先把真实 HTTP 路径跑通。新联调优先使用
[`local-integration-sandbox.md`](local-integration-sandbox.md) 的统一服务；旧
`reference_fact_api.py` 继续保留兼容：

- URL path 替换 `{order_id}`、鉴权 Header、`X-Request-ID` 透传；
- 成功响应、404、401、超时、非 JSON 五类语义；
- 字段白名单、手机号/地址出口脱敏；
- 探测器 5 个场景、181 项检查可重复执行。

它**不替代**厂商 sandbox：不证明厂商字段映射、真实 TLS/网络、限流、真实超时注入或跨主机可达性。

## 2. 一键演练（推荐）

```cmd
cd /d D:\path\to\product-insight-copilot\backend
..\.venv\Scripts\python.exe scripts\verify_fact_local_rehearsal.py
```

预期输出：

```text
order      calls=5 pass_rate=100.00% ...
payment    calls=5 pass_rate=100.00% ...
logistics  calls=5 pass_rate=100.00% ...
aftersale  calls=5 pass_rate=100.00% ...
checks=181/181 overall_p95=...ms
结果=PASS
```

证据写入 `docs/testing/artifacts/fact-integration-local-check.json`，只含场景 ID、字段名、错误码与延迟，
不含 URL、鉴权头、订单号、响应值。

## 3. 两个进程手动联调

终端 A：启动统一参考 API（默认 8091）：

```cmd
cd /d D:\path\to\product-insight-copilot\backend
set SANDBOX_API_KEY=local-sandbox-key
set SANDBOX_WEBHOOK_SECRET=local-webhook-secret
..\.venv\Scripts\python.exe scripts\reference_integration_api.py --port 8091
```

终端 B：配置真实 HTTP 适配器并跑探测器：

```cmd
cd /d D:\path\to\product-insight-copilot\backend
set TICKET_USE_MOCK_ADAPTERS=false
set TICKET_FACT_ORDER_URL=http://127.0.0.1:8091/orders/{order_id}
set TICKET_FACT_PAYMENT_URL=http://127.0.0.1:8091/payments/{order_id}
set TICKET_FACT_LOGISTICS_URL=http://127.0.0.1:8091/logistics/{order_id}
set TICKET_FACT_AFTERSALE_URL=http://127.0.0.1:8091/aftersales/{order_id}
set TICKET_FACT_AUTH_HEADERS={"X-Api-Key":"local-sandbox-key"}
set TICKET_FACT_TIMEOUT_SECONDS=0.25
set TICKET_FACT_MAX_RETRIES=0
set FACT_PROBE_SUCCESS_ORDER_ID=SANDBOX-OK-001
set FACT_PROBE_SUCCESS_CUSTOMER_ID=SANDBOX-CUST-001
set FACT_PROBE_NOT_FOUND_ORDER_ID=SANDBOX-NOTFOUND-001
set FACT_PROBE_LOGISTICS_TIMEOUT_ORDER_ID=SANDBOX-TIMEOUT-001
set FACT_PROBE_AUTH_ORDER_ID=SANDBOX-AUTH-001
set FACT_PROBE_BADJSON_ORDER_ID=SANDBOX-BADJSON-001
..\.venv\Scripts\python.exe scripts\verify_fact_integrations.py --plan ..\docs\integrations\fact-probe-plan.local.json --allow-real-fact-adapters --runs 1 ^
  --json-out ..\docs\testing\artifacts\fact-integration-local-check.json
```

## 4. 参考服务提供的场景

| 订单号 | 行为 |
|---|---|
| `SANDBOX-OK-001` | 四类来源成功；含手机号/地址/未知字段，用于验证脱敏与字段白名单 |
| `SANDBOX-NOTFOUND-001` | 四类来源 404 → `order_not_found` |
| `SANDBOX-TIMEOUT-001` | 物流延迟 0.6s → `adapter_timeout`（适配器 0.25s 超时） |
| `SANDBOX-AUTH-001` | 四类来源 401 → `adapter_auth_error` |
| `SANDBOX-BADJSON-001` | 四类来源返回非 JSON → `adapter_invalid_response` |

## 5. 平台方对接清单

请平台方按顺序确认：

1. 四个 **只读 GET** 端点，URL 含 `{order_id}`，生产 HTTPS；
2. 鉴权只走服务端 Header，不把密钥写进 URL；提供最小权限账号与 IP 白名单；
3. 回传 `X-Request-ID`（Header 或响应体 `request_id`）；
4. 成功响应为 `{"data": {...}, "observed_at": "ISO-8601 带时区", "request_id": "..."}`；
5. `data` 只含契约允许字段；手机号/地址由平台或适配器出口脱敏；
6. 明确 404 / 401 / 408 / 429 / 5xx 的语义与重试约定；
7. 提供 4 类匿名测试标识：成功、订单不存在、物流超时、鉴权失败（可选非 JSON）；
8. 确认调用配额、超时上限、日志留存与删除策略；
9. 不提供退款、赔付、改价、补发等写接口；本系统只读事实。

## 6. 什么时候算"真实外部证据"

本地回环演练通过只说明**适配器实现正确**，不能解除发布门禁。真实外部证据仍然要求：

- 厂商 sandbox 的四个只读端点与匿名测试订单；
- 成功、404、超时（平台可注入）等场景至少 3 轮；
- 输出固定证据 `docs/testing/artifacts/fact-integration-result.json`；
- 预发布报告读取该文件并在 24 小时新鲜度内校验。

见 `docs/product-redesign/售后预发布验收清单.md` 的"外部验收门"。

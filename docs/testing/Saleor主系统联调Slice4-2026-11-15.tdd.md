# Saleor 主系统联调 Slice 4 TDD 证据

日期：2026-11-15
范围：主系统 HTTP 事实链路、故障注入、退款安全门禁、反思与 Trace
证据口径：`local_high_fidelity_sandbox`，不是企业生产证据

## 用户旅程

1. 客服提交十类售后工单时，主系统通过四类只读 HTTP 来源取得同一场景事实并给出符合场景的处理动作。
2. 任一来源超时、限流、缺字段、坏 JSON 或 5xx 时，其他来源事实仍保留，系统不自动承诺退款。
3. 退款、部分退款、重复退款和跨来源冲突必须受金额、审批、反思与人工核验门禁保护。
4. 运维查看 Trace 时能看到来源、适配器、版本、观测时间、请求 ID、模拟标记和错误码。

## RED 证据

- Task Trace 测试最初失败：响应缺少 `fact_sources`。
- 新增“任一事实来源失败阻断退款”后，订单、物流、售后三个参数用例均返回 `refund`，共 3 项失败。
- 验收器测试最初因 `verify_saleor_main_workflow.py` 不存在，共 7 项失败。
- 首次主系统 E2E 为业务 6/10、故障 6/6；暴露反思计数口径错误、部分退款意图缺失和临时库句柄未释放。
- 正常签收场景测试最初返回 `logistics_stalled`；“退还剩余可退金额”最初未命中退款意图。

## GREEN 证据

相关回归：

```cmd
cd /d D:\path\to\product-insight-copilot\backend
..\.venv\Scripts\python.exe -m pytest tests\unit\test_http_fact_adapter.py tests\unit\test_after_sales_agents.py tests\unit\test_ticket_domain.py tests\unit\test_reference_integration_api.py tests\unit\test_verify_saleor_main_workflow.py tests\integration\test_task_flow.py tests\integration\test_checkpointer_recovery.py -q
```

结果：155 通过、0 失败。

主系统 E2E：

```cmd
..\.venv\Scripts\python.exe scripts\verify_saleor_main_workflow.py --sandbox-port 8092 --json-out ..\docs\testing\artifacts\saleor-commerce-sandbox-slice4-local.json
```

结果：业务场景 10/10，技术故障 6/6，`production_ready=false`。

## 测试保证

| # | 保证 | 测试或证据 | 类型 | 结果 |
|---:|---|---|---|---|
| 1 | 空数据和缺关键字段不会被当成成功事实 | `test_http_fact_adapter.py` | 单元 | PASS |
| 2 | 来源版本贯穿持久化、API 和 Trace | `test_ticket_domain.py`、`test_task_flow.py` | 单元/集成 | PASS |
| 3 | 部分退款只使用剩余可退金额 | `test_after_sales_agents.py`、Slice 4 机器证据 | 单元/E2E | PASS |
| 4 | 重复退款与跨来源冲突不生成退款 | `test_after_sales_agents.py`、Slice 4 机器证据 | 单元/E2E | PASS |
| 5 | 任一来源失败保留其他来源并阻断退款承诺 | `test_after_sales_agents.py`、六类故障 E2E | 单元/E2E | PASS |
| 6 | 退款仍需主管审批 | Slice 4 机器证据 | E2E | PASS |
| 7 | 反思执行且修订次数不超过配置 | Trace agent runs、Slice 4 机器证据 | E2E | PASS |
| 8 | 证据制品不含事实值、订单映射、Token、GraphQL 或 PII | `test_verify_saleor_main_workflow.py` | 单元 | PASS |

## 覆盖率

本机共享 NumPy 来自另一个虚拟环境，`pytest-cov` 直接启动会触发 Windows DLL 重入错误；
因此先导入 NumPy，再启动 Coverage API 和 pytest。实测结果：

- `app.adapters.http`：87%；
- `sandbox_services.app`：93%；
- `app.agents.after_sales`：76%；
- 三个核心改造模块合计：81%。

## 已知边界

- 使用 Stub 模型，证明确定性编排与安全门禁，不证明真实模型质量。
- 使用 Saleor 本地合成订单与显式 local overlay，不是企业 OMS、真实支付或真实快递数据。
- 未验证企业网络、真实凭据轮换、外部 SLA、跨主机故障切换或真人客服体验。
- `enterprise_endpoint_verified=false`、`real_payment_verified=false`、
  `real_carrier_verified=false`、`production_ready=false`。

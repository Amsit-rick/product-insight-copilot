# Saleor 评测与交付 Slice 5 TDD 证据

日期：2026-11-16
来源任务：`docs/product-redesign/Saleor高保真业务Sandbox改造任务-2026-11-15.md`

## 用户旅程

- 作为开发或验收人员，我希望一份机器报告直接给出成功率、P50/P95、失败类型和人工接管原因，
  以便不打开原始业务数据也能判断本地联调质量。
- 作为交付审核人，我希望静态 Sandbox、Saleor 本地高保真 Sandbox 和企业真实端点明确分层，
  以免把本地合成证据误写成生产事实。

## RED / GREEN

| 阶段 | 命令 | 结果 |
|---|---|---|
| RED | `..\.venv\Scripts\python.exe -m pytest tests\unit\test_verify_saleor_main_workflow.py -q` | 8 通过、3 失败；缺少 `_percentile`、`summarize_results` 和延迟参数 |
| GREEN | 同上 | 11 通过、0 失败 |
| 相关回归 | `..\.venv\Scripts\python.exe -m pytest tests\unit\test_http_fact_adapter.py tests\unit\test_after_sales_agents.py tests\unit\test_ticket_domain.py tests\unit\test_reference_integration_api.py tests\unit\test_verify_saleor_main_workflow.py tests\integration\test_task_flow.py tests\integration\test_checkpointer_recovery.py -q` | 158 通过、0 失败 |
| E2E | `..\.venv\Scripts\python.exe scripts\verify_saleor_main_workflow.py --sandbox-port 8092 --json-out ..\docs\testing\artifacts\saleor-commerce-sandbox-slice5-local.json` | 业务 10/10、故障 6/6 |

## 测试保证

| # | 保证 | 测试/证据 | 类型 | 结果 |
|---:|---|---|---|---|
| 1 | 百分位采用仓库既有 nearest-index 口径，空样本返回 0 | `test_percentile_uses_the_repository_nearest_index_convention` | 单元 | PASS |
| 2 | 汇总准确计算总/分组成功率、两类延迟、错误类型和人工原因 | `test_summary_reports_rates_latencies_failures_and_handoff_reasons` | 单元 | PASS |
| 3 | 场景结果只暴露安全枚举和延迟，不暴露原始事实 | `test_case_evaluation_exposes_safe_handoff_reason_enums` | 单元 | PASS |
| 4 | Token、订单标识、客户标识、事实 payload 和 GraphQL 正文被证据门禁拒绝 | `test_evidence_rejects_sensitive_or_raw_fact_material` | 单元 | PASS |
| 5 | 主系统使用真实本地 HTTP 读取四来源并完成十类业务场景 | Slice 5 JSON `business_cases` | E2E | 10/10 |
| 6 | 六类故障保留健康来源且不生成不安全退款 | Slice 5 JSON `fault_cases` | E2E | 6/6 |

## 实测指标

- 总成功率：100%。
- 业务场景成功率：100%。
- 故障场景成功率：100%。
- 主流程延迟 P50/P95：338/947 ms。
- 事实查询 Agent 延迟 P50/P95：107/138 ms。
- 失败类型为故障注入中被正确观测的安全错误码，不表示验收失败。
- 指标来自本机 Stub 模型、Inline 执行和本地 Saleor 合成数据，不能外推企业 SLA。

## 覆盖率与已知缺口

Slice 4 已验证三个核心改造模块合计覆盖率 81%；本轮将 11 项单测与真实 E2E 执行合并统计，
`verify_saleor_main_workflow.py` 覆盖率为 95%（263 行语句，12 行未覆盖），相关回归 158 项全部
通过。这是目标验收器覆盖率，不冒充全仓覆盖率。

未完成项：尚无未参与开发人员按文档执行冷启动的真人记录；尚无企业 OMS、真实支付、真实物流、
真实模型或生产网络证据。`production_ready=false`。

## 独立冷启动验收入口补强

第二轮 RED：CI 未校验可选 Commerce Compose，冷启动记录验证器不存在，共 5 项失败；随后实测
发现 CI 静态解析缺少必填插值变量，以及 Windows 原始文件 SHA-256 与 JSON 规范化哈希不同，
分别新增测试复现后修复。

GREEN 结果：

- `test_commerce_sandbox_delivery.py` + `test_validate_saleor_cold_start_record.py`：11/11。
- 扩展 Saleor 相关回归：187 项通过。
- `validate_saleor_cold_start_record.py` 覆盖率：93%（56 行语句，4 行未覆盖）。
- CI Compose 命令使用非生产占位变量完成 `config --quiet`，不会启动 Saleor。
- CLI 按原始文件字节绑定 Slice 5 artifact SHA-256，兼容 Windows `certutil` 结果。

新增入口：

- 模板：`docs/testing/templates/saleor-cold-start-acceptance.template.json`；
- 验证器：`backend/scripts/validate_saleor_cold_start_record.py`；
- 运行说明：`docs/integrations/local-integration-sandbox.md`。

验证器拒绝开发者自证声明、既有数据卷、步骤缺失、健康异常、证据哈希不匹配和任何企业/生产
就绪声明。它只能验证声明和机器证据的一致性，不能证明填表人身份，所以外部真人记录仍未完成。

## 2026-11-16 最终开发者验收

本节记录提交前的开发者全量验收，不属于未参与开发人员执行的独立冷启动验收。

| 验收项 | 结果 |
|---|---|
| 后端全量测试 `python -m pytest tests -q` | PASS；全量用例通过，1 项既有 skip，无失败 |
| 前端 `npm run typecheck` | PASS |
| 前端 `npm run build` | PASS；仅出现 `@vueuse/core` 的第三方 Rolldown 注释警告，不影响产物生成 |
| 默认、Release、Saleor Commerce 三套 Compose 静态解析 | PASS |
| Saleor Sandbox 健康检查 | `fact_provider_status=saleor_ready`、`scenario_manifest_status=ready` |
| 主系统 Saleor E2E | 业务场景 10/10、故障场景 6/6，总成功率 100% |

最新本机验收样本中，主流程延迟 P50/P95 为 538/1000 ms，事实查询 Agent 延迟 P50/P95 为
167/239 ms。该结果来自本地高保真 Sandbox、Stub 模型和本机环境，只用于验证链路正确性，
不构成企业生产 SLA。

全量测试首次执行发现全新安装路径的 `001_init.sql` 未包含 `source_version`，而增量安装已通过
`016_order_fact_source_version.sql` 补列。现已将该字段同步到初始迁移，确保新建库与升级库结构
一致。另一次根路径静态页面测试返回 404，是前端 `vite build` 与后端测试并行时短暂清空
`frontend/dist` 造成的验收竞态；构建完成后串行复跑通过，不是运行态路由缺陷。

真实性边界保持不变：`environment=local_high_fidelity_sandbox`、
`enterprise_endpoint_verified=false`、`real_payment_verified=false`、
`real_carrier_verified=false`、`production_ready=false`。Saleor 专项仍为 **99%**；本地工程、测试
和文档已完成，剩余 1% 是未参与开发人员执行并留档的真实冷启动验收。

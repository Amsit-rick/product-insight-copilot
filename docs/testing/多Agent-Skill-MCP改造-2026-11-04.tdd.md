# 多 Agent、Skill 与 MCP 改造 TDD 证据

来源计划：[多Agent-Skill-MCP改造任务-2026-11-03.md](../product-redesign/多Agent-Skill-MCP改造任务-2026-11-03.md)

## 本轮用户路径与保证

| 路径/行为 | 测试 | 类型 | 结果 |
|---|---|---|---|
| 客服可读取业务进度，但不能读取成本与 Token 明细；工程角色可以读取 | `backend/tests/integration/test_ticket_observability_api.py` | 集成 | 通过 |
| MCP 请求只调用固定只读映射，并可透传可信执行上下文 | `backend/tests/unit/test_mcp_fact_adapter.py` | 单元 | 通过 |
| MCP 超时经过有界重试后返回空事实和结构化错误 | `backend/tests/unit/test_mcp_fact_adapter.py` | 单元 | 通过 |
| MCP 响应只接受结构化契约；超大或非法响应不作为事实写入 | `backend/tests/unit/test_mcp_fact_adapter.py` | 单元 | 通过 |
| MCP 事实只保留允许字段，响应中的任意工具名不参与调用路由 | `backend/tests/unit/test_mcp_fact_adapter.py` | 单元 | 通过 |
| MCP endpoint 校验禁止远端明文 HTTP；健康状态不泄露 URL/鉴权值 | `backend/tests/unit/test_mcp_fact_adapter.py` | 单元 | 通过 |

## RED / GREEN 证据

- RED：`python -m pytest backend/tests/unit/test_mcp_fact_adapter.py -q` 在实现前因 `ModuleNotFoundError: app.adapters.mcp` 失败，原因是目标适配器尚不存在。
- GREEN：`.venv\Scripts\python.exe -m pytest backend\tests\unit\test_mcp_fact_adapter.py -q`，7 项通过。
- 回归：`.venv\Scripts\python.exe -m pytest backend\tests -q`，全量后端测试通过，退出码 0。
- 前端：`npm run typecheck`、`npm run build` 通过；构建有既存 `@vueuse/core` `INVALID_ANNOTATION` 警告。
- 静态检查：`node --check frontend/e2e/after-sales.mjs` 与 `git diff --check` 通过。

## 限制与未覆盖项

- 浏览器 E2E 未运行：隔离临时库后端健康检查通过，但桌面安全策略拒绝启动带 CDP 远程调试端口的 Edge；未尝试绕过策略，临时后端已关闭。
- 没有真实 MCP 服务契约或传输客户端；当前验证仅覆盖本地适配器契约，不能证明远端 MCP 可连接或满足生产 SLA。
- 退款、补发等写工具不在映射中；远端返回内容只按事实对象处理，不用于创建工具或权限。
- 全量测试保留既存 Starlette 422 弃用和测试短 JWT 密钥警告，未在本轮扩展处理。

## 2026-11-04 浏览器验收与事实源故障修复补充

| 场景/行为 | 验证方式 | 结果 |
|---|---|---|
| 退款场景展示 7 个业务角色、金额引用支付实付事实、审批前阻止发送 | 隔离 SQLite + stub 模型 + 独立浏览器上下文 | 通过：金额 89 元，状态等待主管审批 |
| 外部事实源未配置时不得编造事实，并将工单转人工核实 | 隔离 SQLite + 未配置 HTTP 事实端点 + 独立浏览器上下文 | 通过：显示“待人工核实”，下一步“人工核实事实来源”，详情明示事实源不可用 |
| 优惠场景展示 4 个业务角色并生成草稿 | 隔离 SQLite + stub 模型 + 独立浏览器上下文 | 流程部分通过：协作路径和订单事实正确；答复草稿仍为“正在核实”，未解释已核实的优惠结果，不能视为可直接使用 |
| 重试事实查询失败后，详情刷新并持久化最新阻塞原因 | 新增集成测试 `test_retry_with_unavailable_fact_source_refreshes_manual_handoff`，并浏览器复验 | 通过：失败响应后刷新队列/计数/详情；重复处于等待状态时更新 `missing_fields` 和人工下一步 |
| 测试不得覆盖工作区已有 SQLite 数据库 | 集成测试启动与结束 | 通过：改为进程独占临时目录，退出前 dispose 数据库连接；未触碰 `backend/.tmp/itest.db` |

- 定向后端测试：**48 项通过**；后端全量测试：退出码 **0**。
- 前端 `npm run typecheck`、`npm run build` 通过；构建仍有既存 VueUse `INVALID_ANNOTATION` 警告。
- 三条浏览器路径使用独立临时数据库；8000/8010/8011 实例未参与数据写入。
- 下一步：修复优惠场景在事实已齐全但政策未命中时的业务答复，并重新执行优惠路径浏览器验收。

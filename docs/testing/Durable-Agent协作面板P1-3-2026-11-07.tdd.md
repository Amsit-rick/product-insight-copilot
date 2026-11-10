# Durable Agent 协作面板 P1-3 TDD 证据

日期：2026-11-07

## 来源与完成标准

本轮按 Durable Agent 路线图推进 P1-3，直接消费 P0-3 的实时快照和 P1-1 的持久化任务树，不在前端伪造 Agent 数据。完成标准：

1. 任务进度页显示真实 Agent 协作单元；
2. 第一屏能回答“谁在处理、负责什么、处理到哪里、使用了什么依据”；
3. 人工等待、失败和完成状态使用业务语言表达；
4. 模型、Agent code、owner ID、重放策略和副作用级别默认折叠；
5. 原有时间线、Agent 表格、工具表格和事件流继续保留；
6. 桌面 1440×1000 和移动端 375×812 均可读且不发生协作面板横向溢出；
7. 前端构建、浏览器冒烟、可访问性和后端任务树/快照回归通过。

## 用户旅程

- 作为产品经理，我进入任务进度页后，不需要理解节点名和 Trace 就能知道各 Agent 的职责和完成情况。
- 作为审核人员，我能直接看到任务为什么在等待，以及需要人工确认什么。
- 作为研发排障人员，我仍可按需展开模型、owner ID、工具重放策略等技术细节。
- 作为移动端临时查看者，我不会因为固定侧栏把任务内容压成不可读的窄条。

## RED / GREEN 证据

先在 `frontend/e2e/smoke.mjs` 增加三个验收断言，首次标准隔离 CI 结果：

```text
进度页显示真实 Agent 协作面板：FAIL
协作面板使用业务化职责说明：FAIL
协作面板技术细节默认折叠：FAIL
汇总：85/88 通过
```

失败详情均为：

```json
{"exists":false,"agentCount":0,"hasSummary":false,"hasResponsibility":false,"detailsCollapsed":false}
```

实现协作面板后，核心三项转为通过，但新增的 375px 几何检查发现全局固定侧栏把主内容压缩到 85px：

```text
核心协作面板：PASS
协作面板适配 375px 视口：FAIL
{"viewport":375,"panelWidth":85,"panelScrollWidth":144,"overflowingUnits":0}
```

修复小屏应用外壳后再次执行：

```text
进度页显示真实 Agent 协作面板：PASS（5 个真实 Agent 单元）
协作面板使用业务化职责说明：PASS
协作面板技术细节默认折叠：PASS
协作面板适配 375px 视口：PASS
{"viewport":375,"panelWidth":309,"panelScrollWidth":307,"overflowingUnits":0}
前端端到端冒烟：88/88 通过
```

第二次运行复用了已存在的完成任务，因此没有计入“stub 环境自动建任务”这一条件项，断言总数为 88；首次 RED 的总数 88 包含自动建任务项。两次统计口径均来自脚本实际输出。

## 实现语义

- 新增 `AgentCollaborationPanel.vue`，递归读取 `snapshot.task_tree` 中的 Agent、工具和审批执行单元。
- Agent 中文名称与职责使用受控映射；未知 Agent 回退到后端执行单元名称，不伪造职责结果。
- 协作概览统计参与、运行、完成、等待和异常数量；当前阶段读取后端 `current_phase`。
- 工具结果通过执行树 `owner_ref_id` 与快照 `tool_calls.id` 关联，显示真实结果摘要。
- 每个 Agent 的模型、代码、执行记录 ID、失败策略和工具重放契约放入默认关闭的原生 `<details>`。
- 任务页首次加载和轮询都刷新实时快照；快照暂时不可用时保留原任务页，不让附加面板阻断基本操作。
- 小屏应用外壳把固定左侧导航变为横向可滚动顶部导航，桌面端布局不变。

## 测试规格

| # | 保证内容 | 测试或命令 | 类型 | 结果 |
|---|---|---|---|---|
| 1 | 真实任务树渲染至少一个 Agent 单元 | `frontend/e2e/smoke.mjs` | 浏览器 E2E | PASS |
| 2 | 面板包含协作概览与业务职责说明 | `frontend/e2e/smoke.mjs` | 浏览器 E2E | PASS |
| 3 | 技术细节存在且默认关闭 | `frontend/e2e/smoke.mjs` | 浏览器 E2E | PASS |
| 4 | 375px 下协作面板无横向溢出 | `frontend/e2e/smoke.mjs` | 浏览器几何 | PASS |
| 5 | 任务树、快照和任务流保持正确 | 4 个相关测试文件 | 后端回归 | 12/12 PASS |
| 6 | Vue/TypeScript 类型正确 | `npm run typecheck` | 静态检查 | PASS |
| 7 | 生产前端可构建 | `npm run build` | 构建 | PASS |
| 8 | 全平台 WCAG A/AA 自动扫描 | `node frontend/e2e/accessibility.mjs` | axe | 14/14 PASS |
| 9 | Python 源码可编译 | `python -m compileall -q backend/app` | 静态检查 | PASS |
| 10 | 本轮限定文件无空白错误 | `git diff --check -- ...` | 差异检查 | PASS |

## 实际验证结果

```text
P1-3 后端相关回归：12/12 通过
后端全量：1181/1181 通过（标准 CI 隔离运行）
前端端到端冒烟：88/88 通过
axe WCAG A/AA：14/14 页面状态通过
前端类型检查：通过
前端生产构建：通过
Python compileall：通过
限定文件 git diff --check：通过
本地常规 preflight：24 项，10 PASS / 14 WARN / 0 FAIL
```

前端构建仍出现 `@vueuse/core` 的 `INVALID_ANNOTATION` 第三方警告，但 Vite 正常生成产物并以退出码 0 结束。

## 浏览器视觉检查

- 1440×1000：Agent 以双列责任单元展示，职责、处理内容、工具证据和状态层级清楚，无截断。
- 375×812：导航改为横向滚动，主内容恢复全宽；协作面板宽 309px、内部滚动宽 307px，无 Agent 单元越界。
- 中文长文本可换行，技术细节默认关闭，审批/失败颜色使用克制的语义色。
- 浏览器冒烟未发现未捕获页面异常。
- 仓库没有旧版像素基线，视觉回归结论为 `INCONCLUSIVE`；这里只能确认本次截图和几何检查通过，不能声称与不存在的基线一致。

截图：

- `backend/.tmp/e2e/02-progress.png`
- `backend/.tmp/e2e/02b-progress-mobile.png`

## 覆盖率与已知缺口

- 当前环境未安装 `pytest-cov`，未生成或虚构覆盖率百分比。
- P2-1 只读主动异常触发器尚未实现。
- 协作面板当前使用轮询刷新快照，尚未直接消费 SSE delta 做局部更新；功能正确，但仍可继续降低请求量。
- 本轮没有新增真实模型、真实事实接口、真实 Webhook、跨主机或真人客服证据，不能提高生产就绪度。

## Git 检查点说明

工作区存在大量此前未提交且相互交织的改造。本轮未执行提交、重置、清理或历史改写，使用本报告保留 RED/GREEN 证据。

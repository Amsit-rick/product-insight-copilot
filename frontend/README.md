# 电商售后工单智能处理系统前端

本前端以客服工单处理为主线，覆盖**工单列表、工单详情、主管质检、策略库和质量复盘**等日常操作；
同时保留工作台、新建诊断、任务进度、诊断报告和知识库管理等历史诊断能力，供管理员和研发排障使用。

历史诊断页面不是客服的默认入口。客服和主管登录后分别进入自己的工单队列，避免把技术诊断信息误当成日常业务数据。

## 两种运行方式

### 1) 作为构建产物，由后端同源托管（最省事）

```powershell
cd frontend
node node_modules\vite\bin\vite.js build      # 产物写入 frontend/dist
```

后端启动时会自动把 `frontend/dist` 挂到根路径（`app/main.py`）。零依赖开发脚本默认使用 8000；售后演示/E2E 文档常用隔离端口 8010，因此直接访问前请以实际启动命令输出为准：

- 界面：<http://127.0.0.1:8000/>
- 接口文档：<http://127.0.0.1:8000/docs>

端口矩阵见 `docs/运行端口说明.md`。

同源访问，**不涉及 CORS**；前端用 hash 路由，所以刷新任意子页面都不需要服务端做 SPA fallback。

### 2) 开发模式（HMR + 接口代理）

```powershell
cd frontend
node node_modules\vite\bin\vite.js        # http://127.0.0.1:5180
```

- 默认端口 **5180**（本机 5173 被另一个项目的开发服务器占用，且 `strictPort: true`：
  端口冲突会直接报错，而不是悄悄换端口——否则会出现"代理指向了别人的后端"这种极难排查的问题）。
- `/api` 与 `/health` 由 Vite 代理到 `VITE_BACKEND`（默认 `http://127.0.0.1:8010`），
  因此开发模式同样不依赖 CORS。

## 目录结构

```text
frontend/
├── index.html
├── vite.config.ts          Vite 配置（别名 @ → src、开发代理、构建输出）
├── tsconfig.json           TS 配置（strict + noUnusedLocals）
└── src/
    ├── main.ts             应用入口：Pinia + Router + Element Plus 图标（组件由 Vite 按需注册）
    ├── App.vue             顶栏（环境信息 / 当前用户 / 角色）+ 侧边导航 + 路由出口
    ├── api/
    │   ├── client.ts       fetch 封装：同源 HttpOnly 会话、CSRF Header、统一错误结构、401 自动回登录
    │   ├── endpoints.ts    按后端路由分组的接口（与 app/api/routes_*.py 一一对应）
    │   └── types.ts        与后端 Pydantic Schema 对应的类型
    ├── stores/session.ts   会话：当前用户、权限动作、可见知识库（不在 JavaScript 中保存令牌）
    ├── router/index.ts     路由与登录守卫（hash 模式）
    ├── components/         StatusTag / RiskTag / CitationMarks / StatementList /
    │                       MetricLineChart（内联 SVG，无图表库）/ WorkflowTimeline / EvidencePanel
    └── views/              Login / Workbench / TaskCreate / TaskProgress / Report /
                            Knowledge / Approval
```

## 页面与后端能力的对应

| 页面 | 用到的接口 |
|---|---|
| 登录 | `POST /auth/session`、`GET /auth/me`、`POST /auth/logout` |
| 工作台 | `GET /tasks`、`GET /tasks/summary`、`POST /tasks/{id}/cancel`、`POST /tasks/{id}/retry` |
| 新建诊断 | `GET /knowledge-bases`、`GET /metrics/definitions`、`POST /tasks` |
| 任务进度 | `GET /tasks/{id}`、`/events`、`/trace`、`GET /workflow`、`POST /tasks/{id}/approve` |
| 诊断报告 | `GET /tasks/{id}/report`、`GET /reports/{id}/export`、`GET /metrics/trend`、`POST /tasks/{id}/approve` |
| 知识库管理 | `GET /knowledge-bases`、`/documents`、`POST /documents`、`DELETE /documents/{id}`、`POST /knowledge-bases/{code}/reindex`、`POST /metrics/import` |
| 人工审核 | `GET /approvals`、`GET /approvals/{id}`、`POST /approvals/{id}/decision` |

界面上的权限入口（新建任务、上传、删除、重建索引、审核按钮）都按 `GET /auth/me` 返回的 `actions`
隐藏；但**真正的裁决始终在后端**——前端隐藏只是体验优化，越权请求照样会被 403。

## 售后角色与技术信息隔离

| 角色 | 默认工作入口 | 日常可见信息 | 技术诊断信息 |
|---|---|---|---|
| 客服（`operation`） | 售后工单 | 客户问题、订单事实、业务化核验进度、建议、政策依据、下一步和回复草稿 | 不显示 Agent 步骤、SSE 节点、Token、Trace ID、工作流代码、审计明细和政策版本诊断 |
| 主管（`reviewer`） | 售后工单、人工审核、质检抽查 | 客服信息 + 审批、质检和模板管理 | 默认不进入研发诊断平台，也不显示上述技术诊断信息 |
| 管理员/研发 | 完整平台 | 按权限查看业务与管理功能 | 可展开技术核验过程，并查看排障所需的技术记录 |

路由守卫还会拦截客服和主管直接输入 `/system`、`/evaluations` 等技术页面地址，并送回 `/tickets`。
这是导航层的产品隔离；接口权限仍由后端 RBAC 独立执行，不能依赖前端路由作为安全边界。

工单还区分 `business`、`demo`、`test` 三种数据来源。客服列表、分页、质检列表、导出、指标卡和派单负载默认排除 `test`；需要排障时可勾选“显示测试记录”。演示数据继续显示，但带“演示”标识。生产接口拒绝创建 `demo/test` 来源，避免真实工单被人为隐藏。

客服登录后默认进入“我的工单”，主管默认查看“全部工单”；也可切换到“未分配”。顶部五张处理卡不是装饰性指标：每张卡都会把对应 `attention` 参数发给服务端，打开“即将超时 / 等待回复 / 等待客户补充 / 待主管审批 / 今日已完成”真实分页队列。卡片数字使用相同的 `scope` 范围统计，避免把他人的工单混进个人待办；普通列表默认按未完成、高优先级、行动状态和创建时间排序，已完成项后置。

列表搜索常驻，低频条件收进“更多筛选”，表格按“客户问题 / 处理进度 / 下一步做什么 / 处理人”四类业务信息组织。详情先突出“本单下一步”，再按“01 客户反馈 / 02 已核实的信息 / 03 建议处理 / 04 处理记录”展示；多 Agent 默认只给角色数量与名称，用户主动展开后才显示各角色职责、结论、并行阶段和本次核验重点。

## 客服键盘与读屏基础支持

- 五张处理卡和表格“下一步”使用原生按钮，可通过 Tab 聚焦并用 Enter/Space 操作；焦点状态清晰可见。
- 当前行动队列和操作结果使用礼貌播报区，读屏软件无需移动焦点即可获知筛选和提交结果。
- 工单范围、搜索和筛选控件带业务化可访问名称；详情入口的名称包含工单主题和下一步。
- 登录账号、口令与标签建立关联，表单使用原生提交语义和正确的自动填充属性。
- 系统设置“减少动态效果”时，行动卡不再播放状态过渡。

`npm run test:a11y` 会在真实浏览器中注入固定版本 `axe-core`，默认扫描登录页、客服工单列表、客服工单详情和主管质检页四个核心状态；当前四个状态均为 0 条 WCAG A/AA 自动规则违规。设置 `A11Y_SCOPE=all` 后还会扫描历史诊断平台的工作台、新建诊断、知识库、人工审核、指标、系统、评测、密级策略、审计和问答页面；当前全平台 14/14 个页面状态通过。这个结果不是完整 WCAG 认证：人工屏幕阅读器走查、放大/高对比度人工检查和真人无指导测试仍需单独执行。

## 说明与已知限制

- **依赖以离线复用为主**：`node_modules` 最初从机器上已有的 Vue 3.5 + Vite 8 + Element Plus 2.14
  工程复制而来；`vue-tsc` 等新增依赖已写入 `package.json` / `package-lock.json`，后续按
  `npm install` 即可复现（无网络时继续复用现有 `node_modules`）。
- 已安装 `vue-tsc`，`npm run typecheck` 会做 **`.ts` + `.vue` 模板**的完整类型检查
  （`node_modules\vue-tsc\bin\vue-tsc.js --noEmit -p tsconfig.json`，当前 0 错误）；
  `*.vue` 模块由 Volar 解析，不再用 `env.d.ts` 的通用声明降级。
- **Element Plus 按需引入**：`unplugin-vue-components` + `ElementPlusResolver` 自动注册模板里的 `el-*`，
  主包从约 **1.16 MB**（gzip 369 KB）降到约 **430 KB**（gzip 140 KB）。新增组件直接写 `el-*` 即可，
  不需要在 `main.ts` 全局 `app.use(ElementPlus)`；`ElMessage` 等服务式 API 仍显式 import，样式继续用全局 `index.css`。
- 图表用**内联 SVG**手写（`MetricLineChart.vue`）：指标趋势只需要一条折线，无需引入 ECharts；
  要换图表库时只改这一个组件。
- 报告页的指标曲线是**实时调用受控指标接口**取数（报告本身只保存指标引用与数据版本，不保存行数据），
  因此历史报告在数据版本更新后，曲线会反映当前库中的数据——这一点在“数据版本”标签里有明确标注。

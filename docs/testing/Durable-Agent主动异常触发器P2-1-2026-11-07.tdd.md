# Durable Agent 主动异常触发器 P2-1 TDD 证据

日期：2026-11-07

## 目标与安全边界

本轮完成路线图最后一项 P2-1：默认关闭、只读、可审计的主动异常触发器。它只负责从标准化指标快照中发现候选异常，不把“发现异常”直接等同于“允许 Agent 执行动作”。

1. 默认配置 `PROACTIVE_ANOMALY_ENABLED=false`，关闭时扫描接口失败关闭；
2. 自动阶段只读取指标快照并写入候选运行告警；
3. 自动阶段不调用模型、不启动 Agent、不创建诊断任务、不访问外部网络；
4. 自动阶段不执行退款、改价、消息发送或其他业务副作用；
5. 只有具备 `admin:manage` 权限的管理员才能查看状态、发起扫描或确认候选；
6. 管理员明确确认后，才使用现有任务服务创建并派发多 Agent 诊断；
7. 同一租户、指标、日期和异常方向使用稳定指纹去重；
8. 同一候选告警通过 `proactive-alert:{alert_id}` 幂等创建诊断任务；
9. 扫描、候选发现和人工确认均写入审计；
10. API 进程内不启动定时线程，避免多副本重复调度。后续由单实例调度器显式调用扫描接口。

## RED / GREEN

先增加服务层和 API 层测试，首次运行失败：

```text
ModuleNotFoundError: No module named 'app.services.proactive_anomaly'
```

完成服务、配置、路由和前端管理入口后，P2-1 原始测试转绿：

```text
backend/tests/unit/test_proactive_anomaly_trigger.py
backend/tests/integration/test_proactive_anomaly_api.py
8/8 passed
```

浏览器首次加入断言时，断言被错误放在 PM 账号的系统状态阶段，因此失败：

```text
[FAIL] 系统状态显示默认关闭的只读主动异常触发器
```

根因不是接口或页面缺失，而是主动触发器位于管理员运行告警区。将断言移动到 admin 登录后的系统状态页后，完整冒烟转绿，同时保留“PM 看不到管理员运维区”的权限断言：

```text
[PASS] 系统状态显示默认关闭的只读主动异常触发器（admin）
[PASS] 队列运维对 pm 不显示（仅管理员）
前端端到端冒烟：89/89 通过
```

## 实现内容

### 后端

- 新增 `backend/app/services/proactive_anomaly.py`。
- 新增 `PROACTIVE_ANOMALY_ENABLED`、`PROACTIVE_ANOMALY_METRICS`、`PROACTIVE_ANOMALY_LOOKBACK_DAYS`、`PROACTIVE_ANOMALY_METHOD` 和 `PROACTIVE_ANOMALY_THRESHOLD` 配置。
- 新增管理员 API：
  - `GET /api/v1/operational-alerts/proactive-trigger`
  - `POST /api/v1/operational-alerts/proactive-trigger/scan`
  - `POST /api/v1/operational-alerts/{alert_id}/create-diagnosis`
- 扫描复用现有指标异常检测服务，候选告警保存数据集版本、来源版本、质量标记、日期窗口和阈值。
- 候选确认复用现有 `TaskService` 和任务派发链，强制进入人工复核语义。

### 前端

- 系统状态页管理员运行告警区新增“只读主动异常触发器”。
- 第一屏明确显示默认关闭、扫描指标、窗口、方法和阈值。
- 页面明确说明“不调用模型、不自动启动 Agent、管理员确认后才创建诊断任务”。
- 候选告警支持“创建诊断任务”和“查看诊断任务”；重复确认不会重复创建任务。

## 测试规格

| # | 保证内容 | 类型 | 结果 |
|---|---|---|---|
| 1 | 默认关闭且关闭时不读写候选、任务或工具调用 | 单元/API | PASS |
| 2 | 只读扫描只创建候选告警，不创建任务 | 单元 | PASS |
| 3 | 相同异常候选稳定去重 | 单元 | PASS |
| 4 | 人工确认幂等创建一个诊断任务 | 单元/API | PASS |
| 5 | 非主动异常告警不能转诊断 | 单元 | PASS |
| 6 | 扫描和确认仅管理员可用 | API | PASS |
| 7 | 管理员页面显示默认关闭和安全边界 | 浏览器 E2E | PASS |
| 8 | PM 页面不暴露管理员运维入口 | 浏览器 E2E | PASS |

## 实际验证结果

```text
P2-1 原始专项：8/8 通过
主动触发器、运行告警与任务流相关回归：23/23 通过
后端全量：1189/1189 通过
前端端到端冒烟：89/89 通过
axe WCAG A/AA：14/14 页面状态通过
前端 TypeScript 类型检查：通过
前端生产构建：通过
Python compileall：通过
限定文件 git diff --check：通过
常规 preflight：24 项，10 PASS / 14 WARN / 0 FAIL
```

前端构建仍有 `@vueuse/core` 的第三方 `INVALID_ANNOTATION` 警告，但构建以退出码 0 完成并生成生产产物。

## 已知边界

- 当前环境未安装 `pytest-cov`，没有生成或虚构覆盖率百分比。
- 本轮验证使用本地 SQLite、stub 模型和演示指标，不代表真实生产指标质量或模型效果。
- 当前没有运行生产级单实例调度器；触发器能力通过显式扫描 API 提供，默认关闭。
- 真实模型、真实事实接口、外部 Webhook、跨主机生产环境和真人客服验收仍未完成。
- `production_ready=false`，本轮不得提升生产交付准备度。

## Git 检查点说明

工作区存在大量此前未提交且相互交织的改造。本轮未执行提交、重置、清理或历史改写，使用本报告保留 RED/GREEN 和权限场景修复证据。

# Durable Agent 单实例主动调度器 P3-1 TDD 证据

日期：2026-11-07

## 目标与完成标准

本轮把 P2-1 的手工只读扫描入口接入独立调度进程，补齐生产形态所需的单实例互斥和运行状态观测，但不扩大自动化权限。

1. API 进程和 worker 不启动定时扫描线程；
2. 调度器作为独立进程运行，默认关闭；
3. Docker Compose 只有显式启用 `proactive` profile 才创建调度服务；
4. 多实例误启动时通过数据库租约保证同一时刻只有一个实例扫描；
5. 租约过期后允许其他实例接管，只有当前 owner 可以续租、记录结果和释放；
6. 启用后扫描所有 `status=active` 的租户，单租户失败不阻断其他租户；
7. 每个租户的成功或失败都写入审计；
8. 自动阶段只生成候选告警，不调用模型、不启动 Agent、不创建诊断任务；
9. 管理员可以查看最近运行状态，但接口不暴露 owner ID 或内部错误正文；
10. 数据库迁移未完成时调度器失败关闭，不带病启动。

## RED / GREEN

先增加租约和调度行为测试，首次运行失败：

```text
ModuleNotFoundError: No module named 'app.models.scheduler'
```

增加管理员调度状态断言后，API 首轮失败：

```text
KeyError: 'scheduler'
```

完成租约模型、迁移、独立进程、Compose profile 和状态接口后转绿：

```text
调度器/容器首轮：13/13 通过
租约丢失、租户隔离和状态脱敏加固后：16/16 通过
调度、迁移、告警与 API 相关回归：57/57 通过
```

## 实现内容

### 数据库租约

- 新增 `scheduler_leases` 表和 `SqlSchedulerLease` 服务。
- 租约记录 owner、心跳、过期时间、最近运行时间、状态、结果和错误标志。
- 获取租约使用数据库条件更新；过期租约可被接管。
- 非当前 owner 无法续租、写结果或释放租约。
- 全新安装基线和增量迁移同步维护，SQLite 存量库可通过幂等初始化补表。

### 独立调度进程

- 新增 `python -m app.proactive_scheduler` 入口。
- 支持持续运行、`--once` 单轮验证、`--rounds` 有界运行和 SIGINT/SIGTERM 退出。
- 启用前等待 `scheduler_leases` 表就绪，超时明确失败。
- 每轮获取数据库租约；每个租户扫描前续租，租约丢失立即停止本轮。
- 单租户异常被隔离并审计，不影响后续活跃租户。
- 默认关闭时安全退出，不要求数据库迁移表存在。

### Compose 与可观测性

- 新增 `proactive-scheduler` 服务，profile 为 `proactive`，不暴露端口。
- 调度服务等待 Postgres 健康和 migrate 成功，共用应用镜像与环境变量。
- 系统状态页显示是否正在扫描、最近结果、候选数量和最近完成时间。
- API 只返回 `running`、时间、状态、错误布尔值和脱敏结果，不返回 owner ID 或异常正文。

## 测试规格

| # | 保证内容 | 类型 | 结果 |
|---|---|---|---|
| 1 | 默认关闭时进程安全退出 | 单元 | PASS |
| 2 | 迁移表缺失时启用进程失败关闭 | 单元 | PASS |
| 3 | 同时只有一个 owner 获得租约 | 单元 | PASS |
| 4 | 过期租约可被其他 owner 接管 | 单元 | PASS |
| 5 | 非 owner 不能续租、写结果或释放 | 单元 | PASS |
| 6 | 活跃租户逐一扫描，失败租户不阻断其他租户 | 单元 | PASS |
| 7 | 租约丢失后停止继续扫描 | 单元 | PASS |
| 8 | 自动扫描只生成候选，不创建诊断任务 | 单元/集成 | PASS |
| 9 | 每租户扫描结果进入审计 | 单元/集成 | PASS |
| 10 | 管理员状态接口不泄露 owner 和错误正文 | API | PASS |
| 11 | Compose 默认不启动调度器，profile 显式启用 | 契约测试 | PASS |
| 12 | 管理员页面展示独立调度状态 | 浏览器 E2E | PASS |

## 实际进程验证

在隔离 SQLite 数据库和演示指标上执行：

```text
python -m app.proactive_scheduler --once
status=completed
tenants_total=1
tenants_succeeded=1
tenants_failed=0
metrics_scanned=2
anomalies_found=17
```

数据库复核：

```text
candidate_alerts=17
alerts_without_task=17
proactive_tasks=0
scheduler_status=completed
scheduler_owner_released=True
```

该结果证明本地自动阶段没有创建诊断任务或启动 Agent，不代表真实指标源的异常质量。

## 验证结果

```text
调度器/容器加固专项：16/16 通过
调度、迁移、告警与 API 相关回归：57/57 通过
后端全量：1200/1200 通过
前端端到端冒烟：90/90 通过
axe WCAG A/AA：14/14 页面状态通过
前端 TypeScript 类型检查：通过
前端生产构建：通过
Python compileall：通过
Compose profile 静态解析：通过
常规 preflight：24 项，10 PASS / 14 WARN / 0 FAIL
```

前端构建仍有 `@vueuse/core` 的既有第三方 `INVALID_ANNOTATION` 警告，但构建以退出码 0 完成并生成生产产物。

## 已知边界

- 本轮使用本地 SQLite、stub 模型和演示指标，没有连接真实指标平台。
- 尚未在两台真实主机或生产 PostgreSQL 上进行并发租约竞争和故障接管演练。
- 调度器只解决“何时扫描”和“避免重复扫描”，不证明异常规则本身适合真实业务。
- 真实事实 sandbox、外部 HTTPS Webhook、真实模型 holdout、跨主机环境和真人客服证据仍未完成。
- 当前环境没有 `pytest-cov`，没有生成或虚构覆盖率。
- `production_ready=false`；生产交付准备度不因本地调度器通过而提高。

## Git 检查点说明

工作区存在大量此前未提交且相互交织的改造。本轮未执行提交、重置、清理或历史改写，使用本报告保存实现与验证证据。

# Durable Agent PostgreSQL 调度接管 P3-2 TDD 证据

日期：2026-11-08

## 目标与边界

P3-1 已实现数据库单实例租约，但此前只有 SQLite 进程验证和租约单元测试。本轮补齐真实 PostgreSQL 下的独立进程竞争与崩溃接管演练。

完成标准：

1. 使用隔离 PostgreSQL，应用仓库当前全部迁移；
2. 独立持有者进程成功取得随机租约；
3. 第二个进程在租约有效期内不能取得租约；
4. 强制终止持有者，不执行正常 release；
5. 竞争者在租约到期前仍不能接管；
6. 租约到期后竞争者成功接管并正常释放；
7. 演练只操作随机租约行，结束后该行被删除；
8. JSON 不保存数据库连接串、租约名或 owner ID；
9. 本地通过只记为本地证据，不解除跨主机生产门禁；
10. 同一演练进入发布镜像与远程 CI 工作流定义。

## RED / GREEN

先增加验收脚本和发布报告测试，首次运行失败：

```text
FileNotFoundError: backend/scripts/verify_scheduler_failover.py
```

实现脚本、报告读取和容器交付契约后转绿：

```text
专项单元与交付契约：35/35 通过
```

测试同时保证：未显式授权时不连接数据库；SQLite 不能形成 PostgreSQL 接管证据；带有跨主机或生产就绪虚假声明的本地证据会被拒绝。

## 实现内容

### 验收脚本

- 新增 `backend/scripts/verify_scheduler_failover.py`。
- 数据库连接只从 `SCHEDULER_REHEARSAL_DATABASE_URL` 读取，不进入子进程参数和 JSON。
- 默认不连接数据库，必须显式传入 `--allow-real-database-call`。
- 只接受 PostgreSQL，不接受 SQLite 作为生产形态证据。
- 使用随机租约名和随机 owner token，不触碰正式 `proactive-anomaly` 租约。
- 独立子进程取得租约后被强制终止，父进程验证租约过期前阻塞、过期后接管。
- 最终只删除本轮随机租约行，不读取或修改业务表。

### 发布报告与 CI

- 预发布报告新增 `infrastructure_verification.scheduler_failover`。
- 合法本地证据状态为 `verified_local`，门禁类型为 `informational_local`。
- `cross_host_failover_verified` 和 `production_ready` 必须保持 false，否则证据判定失败。
- Docker 镜像新增验收脚本，镜像构建及脚本存在性检查通过。
- GitHub Actions `production-shape` job 新增 PostgreSQL 调度接管演练并上传脱敏证据。
- 当前只完成 CI 定义和本地等价验证，尚未由仓库 push 实际触发远程 runner。

## 真实 PostgreSQL 演练

本轮创建专用 Compose project `pic-scheduler-rehearsal`，使用独立端口和独立数据卷。迁移结果：

```text
001_init.sql ... 015_scheduler_leases.sql
15/15 个迁移应用成功
```

崩溃接管结果：

```text
[PASS] holder_acquired
[PASS] concurrent_contender_blocked
[PASS] crashed_holder_did_not_release
[PASS] takeover_after_expiry
[PASS] takeover_released
[PASS] rehearsal_row_cleaned
takeover_wait_seconds=3.0
```

数据库复核：

```text
schema_migrations=15
scheduler_rehearsal_rows=0
```

演练完成后，专用 PostgreSQL 容器、迁移容器、网络和数据卷均已删除。脱敏证据保存在 `docs/testing/artifacts/scheduler-failover-local-check.json`。

## 发布报告结果

```text
scheduler_status=verified_local
local_scheduler_failover_verified=true
cross_host_verified=false
production_ready=false
decision=not_ready_machine_gates
```

## 最终回归

```text
专项单元、发布报告与容器交付契约：35/35 通过
后端全量：1205/1205 通过
常规 preflight：24 项，10 PASS / 14 WARN / 0 FAIL
Python compileall：通过
GitHub Actions Workflow YAML 解析：通过
发布镜像构建与脚本存在性检查：通过
调度接管 JSON 脱敏检查：通过
本轮限定 git diff --check：通过
隔离 Compose 容器残留：0
```

当前环境没有 `pytest-cov`，未生成或虚构覆盖率百分比。

## 已知边界

- 本轮是单台 Windows 主机上的独立进程和独立 PostgreSQL 容器，不是跨主机演练。
- 没有验证托管 PostgreSQL 主从切换、网络分区、时钟漂移或数据库连接池重连。
- 没有连接真实指标源，也没有校准真实业务阈值。
- GitHub Actions 工作流尚未由仓库 push 实际执行，不能声称远程 CI 证据已产生。
- 本轮不提高生产交付准备度，`production_ready=false`。

## 清理与可追溯性

- 隔离 Compose project 和数据卷已按精确项目名清理。
- 未执行 Git reset、checkout、clean 或历史改写。
- 工作区存在此前多轮未提交改动，本轮未创建混合提交。

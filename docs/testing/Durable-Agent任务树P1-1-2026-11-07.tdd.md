# Durable Agent 任务树 P1-1 TDD 证据

日期：2026-11-07

## 来源与完成标准

本轮依据已确认的 Durable Agent 路线图推进 P1-1，没有使用外部计划文件。完成标准：

1. 任务、Agent、工具、审批和后台执行拥有持久化父子关系与所有权；
2. 取消与失败传播策略有独立语义，业务任务状态仍由 `AnalysisTask` 负责；
3. 任务树可通过 API 和实时快照读取；
4. 历史播放包包含执行单元，能够还原当时的 Agent、工具、审批归属；
5. PostgreSQL 新装/升级和 SQLite 存量开发库均能获得根执行单元。

## 用户旅程

- 作为任务观察者，我希望看到真实持久化的多 Agent 执行树，而不是前端临时拼装的流程图。
- 作为任务恢复与审计人员，我希望回放包保留执行单元所有权，以便还原任务当时由谁执行、调用了什么工具、经过了什么审批。
- 作为本地开发者，我希望旧 SQLite 数据库升级后自动补齐任务根节点，不需要删除数据库重建。
- 作为系统维护人员，我希望取消和失败传播遵循明确策略，后台独立任务不会被错误级联取消。

## RED / GREEN 证据

### 回放包缺少执行树

新增 `test_回放包包含任务执行树及Agent工具审批所有权` 后首次执行：

```text
....F
KeyError: 'execution_units'
```

修复后：

```text
backend/tests/unit/test_task_replay.py
5 passed
```

### 执行单元 metadata 序列化冲突

相关回归首次执行发现接口失败：

```text
TypeError: Object of type MetaData is not JSON serializable
```

根因是数据库列名 `metadata` 与 SQLAlchemy `Base.metadata` 同名，通用 `as_dict()` 读取到了 ORM 元数据对象。回放边界改为显式使用 `TaskExecutionUnit.metadata_json` 后：

```text
backend/tests/unit/test_task_replay.py
backend/tests/integration/test_task_replay_api.py
6 passed
```

## 测试规格

| # | 保证内容 | 测试或命令 | 类型 | 结果 |
|---|---|---|---|---|
| 1 | 新任务拥有与任务 ID 相同的 workflow 根执行单元 | `backend/tests/unit/test_task_execution_tree.py` | 单元 | PASS |
| 2 | Agent、工具、审批按真实 owner ID 建立父子关系并保持幂等 | `backend/tests/unit/test_task_execution_tree.py` | 单元 | PASS |
| 3 | cascade 取消会传播，independent 后台单元不被取消 | `backend/tests/unit/test_task_execution_tree.py` | 单元 | PASS |
| 4 | 失败传播支持 continue、block_parent、fail_parent | `backend/tests/unit/test_task_execution_tree.py` | 单元 | PASS |
| 5 | `/tasks/{id}/tree` 返回真实 Agent、工具和审批生命周期 | `backend/tests/integration/test_task_tree_api.py` | 集成 | PASS |
| 6 | 实时快照内嵌的 `task_tree` 与独立树接口一致 | `backend/tests/integration/test_task_tree_api.py` | 集成 | PASS |
| 7 | 回放包包含 workflow、Agent、工具和审批执行单元及 metadata | `backend/tests/unit/test_task_replay.py` | 单元 | PASS |
| 8 | 回放快照接口可以序列化并持久化执行单元 | `backend/tests/integration/test_task_replay_api.py` | 集成 | PASS |
| 9 | SQLite 存量任务在 `init_db` 后自动补齐根执行单元 | `backend/tests/unit/test_migrate.py` | 单元/迁移 | PASS |
| 10 | 前端类型包含递归执行树，API 客户端可读取树接口 | `npm run build` | 构建 | PASS |

## 实际验证结果

```text
P1-1 相关回归：60/60 通过
后端全量：1171/1171 通过
前端生产构建：通过
Python compileall：通过
限定文件 git diff --check：通过
```

前端构建出现 `@vueuse/core` 的 `INVALID_ANNOTATION` 第三方警告，但 Vite 正常生成产物并以退出码 0 结束。

## 覆盖率与已知缺口

- 当前环境没有安装 `pytest-cov`，因此未生成或虚构覆盖率百分比。
- P1-2 `expected_revision` 乐观并发控制尚未实现。
- P1-3 Agent 协作过程面板尚未实现；本轮只提供真实后端树、快照契约与前端类型。
- P2-1 只读主动异常触发器尚未实现。
- 未提供新的真实外部环境证据，本轮不能提高生产就绪度。

## Git 检查点说明

工作区包含大量此前未提交且相互交织的项目改造，创建 RED/GREEN 提交会把不属于本轮的改动混入提交。本轮未执行提交、重置、清理或历史改写，改用本报告保存 RED/GREEN 证据。

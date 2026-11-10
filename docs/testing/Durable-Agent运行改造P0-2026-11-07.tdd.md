# Durable Agent 运行改造 P0 测试证据

日期：2026-11-07  
范围：工具重放安全、阶段级恢复元数据、任务 Snapshot + Delta 协议。

## 1. 来源与验收目标

本轮任务来自对 `disler/durable-pi` 的架构对标，未使用外部计划文件。验收目标转换为三个可测试保证：

1. 恢复流程不得自动重放不可逆工具；幂等工具没有稳定键时不得执行。
2. 任务必须持久化当前业务阶段、阶段尝试次数、检查点版本、恢复次数和唤醒原因。
3. 客户端首次连接必须先收到完整任务快照，随后才能接收增量事件；终态任务必须正常结束流。

## 2. RED 证据

| 缺失能力 | RED 结果 |
|---|---|
| 工具重放契约 | `ImportError: cannot import name 'ReplayPolicy'` |
| 阶段恢复服务 | `ModuleNotFoundError: No module named 'app.services.task_runtime'` |
| 不加载任务行的节点阶段更新 | `reflect_report` 执行后仍停留在 `product_agent` |
| SQLite 存量库升级 | 新增工具/任务运行字段未被 `init_db` 补齐 |
| unsafe 审批后的恢复语义 | 已审批的 unsafe 工具仍会在 `replaying=True` 时被执行 |

这些失败均来自预期缺失能力，不是依赖缺失或测试配置错误。

## 3. GREEN 证据

### 3.1 新增专项

```text
.venv\Scripts\python.exe -m pytest -o addopts="" -q \
  backend\tests\unit\test_tool_replay_contract.py \
  backend\tests\unit\test_durable_task_state.py \
  backend\tests\integration\test_task_live_api.py
```

结果：专项全部通过。

### 3.2 核心回归

覆盖工具审计、运行态写入、迁移、checkpointer 恢复、任务主流程和任务回放：

```text
46 passed
```

### 3.3 后端全量

```text
.venv\Scripts\python.exe -m pytest -o addopts="" -q backend\tests
1164 passed in 60.69s
```

### 3.4 前端构建

```text
cd frontend
npm run build
```

结果：生产构建成功。第三方 `@vueuse/core` 仍有既有非阻断 PURE 注解告警。

## 4. 可读保证

| # | 保证 | 测试 | 类型 | 结果 |
|---|---|---|---|---|
| 1 | `safe` 只允许只读工具 | `test_safe_replay_is_reserved_for_read_only_tools` | 单元 | PASS |
| 2 | 幂等工具必须提供稳定幂等键 | `test_idempotent_tool_requires_stable_key_before_execution` | 单元 | PASS |
| 3 | unsafe 工具在恢复上下文中始终禁止执行 | `test_unsafe_replay_requires_persisted_human_approval` | 单元 | PASS |
| 4 | 人工确认后的 unsafe 动作必须作为新的非重放执行 | 同上 | 单元 | PASS |
| 5 | 轻量节点也会持久化业务阶段 | `test_runtime_persists_phase_attempt_checkpoint_and_resume_metadata` | 单元 | PASS |
| 6 | 恢复来源、次数和唤醒原因持久化 | 同上 | 单元 | PASS |
| 7 | SQLite 存量库可幂等补齐新字段 | `test_init_db_upgrades_legacy_sqlite_tool_call_replay_columns` | 单元 | PASS |
| 8 | 快照含阶段、revision、稳定事件序号和工具契约 | `test_snapshot_contains_phase_revision_and_stable_event_sequence` | 集成 | PASS |
| 9 | SSE 首帧是 snapshot，终态正常发送 done | `test_stream_emits_snapshot_before_delta_or_done` | 集成 | PASS |

## 5. 覆盖率与已知缺口

- 当前环境未安装 `pytest-cov`，`--cov` 参数不可用；未伪造覆盖率百分比。
- 全量自动测试为 1164/1164，但这不等同于语句覆盖率达到 80%。
- 当前正式注册工具仍全部是只读工具；unsafe 契约通过测试替身验证，尚无真实退款/写平台接口可做 sandbox 演练。
- SSE 已验证完整快照、终态收尾和 JSON 帧格式；尚未在真实 Nginx/网关下验证长连接超时、代理缓冲和大规模并发重连。
- 本轮未实现父子任务树、`expected_revision` 并发写冲突、Agent 协作可视化面板和主动异常触发器，这些属于后续 P1/P2。
- 工作树包含大量前序未提交改造，未创建 TDD checkpoint commit，避免把既有改动混入本轮提交；RED/GREEN 证据保存在本文件。


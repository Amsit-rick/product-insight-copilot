# Durable Agent 并发控制 P1-2 TDD 证据

日期：2026-11-07

## 来源与完成标准

本轮按 Durable Agent 路线图推进 P1-2，没有使用外部计划文件。完成标准：

1. 取消、重试和两条审批入口支持 `expected_revision`；
2. 版本比较与 revision 增加必须由数据库原子完成，不能使用“先查再普通写”的伪并发控制；
3. 冲突发生在事件、审计、审批、派发和执行树副作用之前；
4. 409 响应包含期望版本与实际版本，前端能提示刷新；
5. 延迟或重复的恢复消息不能再次恢复同一个任务，也不能在 Worker 中反复重试；
6. 未传 revision 的旧客户端保持顺序兼容，并发旧请求仍通过 CAS 只允许一个成功。

## 用户旅程

- 作为操作人员，我希望旧页面提交取消或重试时不会覆盖任务的新状态。
- 作为审核人员，我希望审批结论绑定我实际看到的任务版本，状态变化后必须刷新确认。
- 作为 Worker 运维人员，我希望延迟或重复的恢复消息被识别为过期消息并安全确认，而不是进入无限重试或死信。
- 作为旧客户端使用者，我希望未升级的页面仍能顺序执行操作，但不能绕过数据库并发保护。

## RED / GREEN 证据

首次运行新增测试时，测试在收集阶段按预期失败：

```text
ImportError: cannot import name 'TaskRevisionConflict' from 'app.services.task_service'
```

该失败证明原系统没有独立的 revision 冲突类型和 CAS 能力。实现后首次运行发现审批路由漏引入 `TaskStatus`，目标用例以 `NameError` 暴露，补齐导入后：

```text
backend/tests/unit/test_task_revision_control.py
backend/tests/integration/test_task_revision_api.py
8 passed
```

增加双 Session CAS 与过期恢复消息测试后：

```text
P1-2 核心与任务流：17/17 通过
P1-2 相关回归：63/63 通过
```

## 实现语义

- `TaskService.claim_revision()` 使用带 `task_id + tenant_id + state_revision + allowed_status` 条件的原子 `UPDATE`。
- 成功占用后 `state_revision` 只增加一次；失败时重新读取实际版本并抛出 `TaskRevisionConflict`。
- 取消、重试、任务审批和审批记录入口都在产生副作用前占用 revision。
- 审批派发携带内部 `_expected_revision`，`NodeRuntime.record_resume()` 再次使用 CAS 消费恢复版本。
- 重复/延迟恢复返回 `revision_conflict` 并写入警告事件；Worker 将该消息视为已消费，不进入失败重试。
- 前端工作台、任务进度页、报告页和审核页均提交当前 `state_revision`。

## 测试规格

| # | 保证内容 | 测试或命令 | 类型 | 结果 |
|---|---|---|---|---|
| 1 | 旧 revision 的取消请求在任何事件或状态副作用前失败 | `test_stale_cancel_is_rejected_before_any_side_effect` | 单元 | PASS |
| 2 | 成功取消只占用一次 revision，重复请求被拒绝 | `test_successful_cancel_claims_revision_once_and_rejects_duplicate` | 单元 | PASS |
| 3 | 重试将 revision 精确增加一次 | `test_retry_claims_revision_exactly_once` | 单元 | PASS |
| 4 | 延迟恢复不能重复消费相同 revision | `test_delayed_resume_cannot_reuse_an_already_consumed_revision` | 单元 | PASS |
| 5 | 两个已加载相同版本的独立 Session 只有一个可以成功更新 | `test_two_sessions_that_loaded_the_same_revision_cannot_both_cancel` | 并发/数据库 | PASS |
| 6 | 过期恢复消息写告警事件并安全结束，不抛给 Worker 重试 | `test_workflow_runner_treats_stale_resume_as_consumed_message` | 单元 | PASS |
| 7 | 取消 API 返回结构化 expected/actual revision 冲突 | `test_cancel_api_rejects_stale_revision_and_returns_current_revision` | 集成 | PASS |
| 8 | 旧重试请求不会进入派发器 | `test_retry_api_does_not_dispatch_when_revision_is_stale` | 集成 | PASS |
| 9 | 任务审批入口的旧请求不会恢复工作流 | `test_task_approval_does_not_resume_when_revision_is_stale` | 集成 | PASS |
| 10 | 审批记录入口使用同一 revision 门禁且不修改审批状态 | `test_approval_record_endpoint_uses_the_same_revision_guard` | 集成 | PASS |
| 11 | 前端四个操作页面携带 revision，类型检查与打包通过 | `npm run build` | 构建 | PASS |

## 实际验证结果

```text
P1-2 相关回归：63/63 通过
后端全量：1181/1181 通过
前端生产构建：通过
Python compileall：通过
限定文件 git diff --check：通过
```

前端构建仍出现 `@vueuse/core` 的 `INVALID_ANNOTATION` 第三方警告，但 Vite 正常生成产物并以退出码 0 结束。

## 覆盖率与已知缺口

- 当前环境未安装 `pytest-cov`，未生成或虚构覆盖率百分比。
- P1-3 Agent 协作过程面板尚未实现；当前后端已具备任务树、快照、事件流和冲突契约。
- P2-1 只读主动异常触发器尚未实现。
- 本轮没有新增真实模型、真实事实接口、真实 Webhook、跨主机或真人客服证据，不能提高生产就绪度。

## Git 检查点说明

工作区存在大量此前未提交且相互交织的改造，创建 RED/GREEN 提交会混入不属于本轮的变更。本轮未执行提交、重置、清理或历史改写，使用本报告保留 RED/GREEN 证据。

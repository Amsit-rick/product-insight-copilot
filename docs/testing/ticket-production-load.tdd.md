# 工单生产形态负载验证 TDD 证据

日期：2026-09-25

## 用户旅程

1. 运维人员可用一次性隔离环境证明工单链路实际使用 Postgres、Redis、数据库队列和共享 Postgres checkpoint。
2. 开发人员可通过真实 HTTP 并发提交工单，得到提交与端到端 P95、吞吐、错误率和队列终态。
3. 验收必须证明至少两个独立 worker 实际领取了本轮任务，而不是只证明启动了两个容器。
4. 任一运行时或性能硬门槛失败，脚本必须非零退出，不能产出假 PASS。

## RED / GREEN

| 行为 | RED 证据 | GREEN 证据 |
|---|---|---|
| 生产负载脚本 | 新测试加载 `backend/scripts/benchmark_ticket_queue.py` 时 `FileNotFoundError` | 统计、健康门禁、worker 数、队列清空和退出码 7 条单测通过 |
| 默认共享 checkpoint | Compose 原来为 API/worker 分别配置容器内 SQLite 文件 | Compose 默认 `postgres`，并加入 `langgraph-checkpoint-postgres` 依赖 |
| Postgres saver 生命周期 | 真实 24 单全部 dead，错误为 `OperationalError: the connection is closed`；新增回归测试在 reset 时发现上下文未被持有/关闭 | 保存 context manager 与 saver 同寿命，reset 显式退出；6 单与 24 单 Docker 实跑均通过 |
| worker 冷启动 | worker 早于 API 建表时因 `relation task_queue_items does not exist` 重启 | worker 启动等待 schema，超时明确失败；2 条单测通过 |
| worker 容器健康 | 任务已成功处理，但 Docker 因继承 API HTTP 探针标记 worker unhealthy | Compose 为 worker 禁用不适用的 HTTP 探针，契约测试固定该行为 |

## 测试规格与证据

| # | 保证 | 测试或命令 | 类型 | 结果 |
|---|---|---|---|---|
| 1 | 非 Postgres、缓存降级、非 queue、非共享 Postgres checkpoint 会被拒绝 | `test_ticket_production_load.py` | unit | PASS |
| 2 | 默认拒绝真实 LLM/Embedding，避免负载验收意外产生费用 | `test_health_gate_refuses_real_model_costs_by_default` | unit | PASS |
| 3 | 提交/端到端 P95、错误率、吞吐、worker 与队列终态口径稳定 | `test_percentile_and_report_metrics_include_both_latency_stages` | unit | PASS |
| 4 | worker 数不足或队列未清空时返回非零 | `test_acceptance_fails_for_worker_shortage_or_nonempty_queue` | unit | PASS |
| 5 | Postgres saver 连接不会被临时上下文回收 | `test_postgres_checkpointer_keeps_connection_context_alive` | integration | PASS |
| 6 | worker 会等待 API/迁移完成建表，并有有限超时 | `test_worker_startup.py` | unit | PASS |
| 7 | 真实 Postgres/Redis/API/两个 worker 完成 24 单 | `benchmark_ticket_queue.py --count 24 ...` | Docker integration/load | PASS |

## 真实负载结果

- 隔离项目：`pic-ticket-load-20260925`，使用独立容器与卷。
- 模型：LLM/Embedding 均为 stub，费用为 0；结果不代表真实模型性能。
- 24/24 提交，24/24 完成，0 错误。
- 提交 P95：2470.97 ms。
- 端到端 P95：3334.72 ms。
- 吞吐：2.38 单/秒。
- 两个实际 worker：`041ee905d01e-1`、`5c54ddb41caa-1`。
- 最终队列：pending=0、claimed=0、dead=0（done=30，含前置 6 单 smoke）。
- 原始报告：`docs/testing/artifacts/ticket-production-load-20260925.json`。

## 已知边界

- 当前门槛证明基础设施和 stub 工作流链路，不证明真实模型的 P95、质量或成本。
- 队列仍基于 Postgres 表，尚未验证更大规模吞吐下是否需要 Redis Stream / SQS / Kafka。
- worker 没有独立 HTTP 指标端点；当前通过 API `/health` 队列统计、队列运维接口和负载脚本观测。

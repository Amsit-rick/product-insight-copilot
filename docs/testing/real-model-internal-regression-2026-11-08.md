# 真实模型内部回归证据（50 单）

日期：2026-11-08
证据文件：`docs/testing/artifacts/real-model-internal-50.json`
脚本：`backend/scripts/run_ticket_evaluation.py --sample 50 --allow-real-models`

## 1. 口径与边界

- 数据集：`backend/tests/evaluation/tickets.json`，**项目内部生成**的 228 条回归集，按 18 个
  验收维度分层抽 50 条（`--sample 50`，组间轮转、组内原序，结果可复现）。
- 模型：`deepseek-chat`（`LLM_PROVIDER=openai_compatible`），真实调用；245 次调用全部
  `model_used=true`，无未计价模型。
- 事实：`TICKET_USE_MOCK_ADAPTERS=true`，订单/支付/物流/售后为 mock 夹具。
- Embedding：stub 确定性哈希向量（不是生产语义向量）。
- 轮次：单轮（`--runs 1`）。**一致性 1.0 是单轮定义值，不代表多轮稳定性。**
- 这不是"独立人工标注 holdout"，不能写成生产质量；生产基线要求见
  [real-model-baseline.md](real-model-baseline.md) 与
  [holdout-annotation-guide-2026-11-08.md](holdout-annotation-guide-2026-11-08.md)。

## 2. 最终结果

| 指标 | 结果 |
|---|---|
| 用例数 | 50 |
| 通过 | **50** |
| 通过率 | **1.00** |
| 问题分类 Macro-F1 | **1.00** |
| 分类准确率 | **1.00** |
| 动作准确率 | **1.00** |
| 风险等级准确率 | **1.00** |
| 审批门禁召回 | **1.00** |
| 失效政策泄漏率 | **0.00** |
| 租户隔离率 | **1.00** |
| 重复写操作拦截率 | **1.00** |
| 政策版本召回 | **1.00** |
| 证据支持率 | **1.00** |
| 端到端 P95 | **13,207 ms** |
| 端到端均值 | 7,545.3 ms |
| 模型调用 | 245 次 |
| Token | 输入 130,785 / 输出 52,689 / 缓存命中 74,541 |
| 总成本 | **¥0.57127** |
| 单均成本 | **¥0.011425** |

成本按项目默认计费表 `deepseek-chat = 2 / 8 / 0.5 元每百万 token`（输入/输出/缓存命中）；
输入/输出与 DeepSeek 公开高峰价一致，缓存命中价 0.5 高于公开价 0.04，因此总成本略偏高、属于保守口径。

## 3. 从 0.94 到 0.78 再到 1.0：一次"加知识反而变差"的回归

这轮最重要的不是 1.0，而是过程中暴露的**检索退化**：

1. **0.94（44/50 → 47/50）**：补齐超时断言后，只剩 3 条 `promotion` 失败——演示政策库没有
   促销政策，系统没有编造引用，而是安全升到 L2（`staff_confirm`）。
2. **0.78（39/50）**：补上促销政策后，`damaged/reship/refund` 反而有 11 条拿不到政策证据。
   根因不是模型，而是**检索被新政策挤占**：政策库从 19 个切片增加到 24 个，通用查询
   `damaged 处理规则 补偿标准` 的 top-5 被新政策里的"处理/补偿/退款"通用词占满，真正含
   "破损/补发"的物流政策被挤出候选；同时 `policy_rag_agent` 的 `data_gaps` 偶尔返回字符串，
   Schema 校验失败会把整段 policies/findings 一起丢弃。
3. **1.0（50/50）**：
   - 按问题类型注入中文主题词：`damaged 破损 损坏 包装 ...`、`promotion_not_applied 满减 促销活动 ...`；
   - 政策检索 `top_k` 从 5 提高到 8，先多召回再按主题词过滤；
   - `BaseAgent` 对 `data_gaps` 做通用归一化（字符串 → 单元素列表），不再因诊断字段丢整段输出。

**教训**：知识库不是"越大越好"；语料增加会改变 top-k 排序，检索质量对语料规模**不单调**。
加政策/文档后必须重跑评测，并检查"原本通过的场景是否被挤掉"。

## 4. 真实模型输出健壮性

- `review_agent` 的 issues 字段别名问题已修复：10 单试点中 4 单 Schema 失败 → 归一化后 0 失败，
  通过率 0.80 → 0.90。
- `policy_rag_agent` 的 `data_gaps` 字符串问题已修复：最终 50 单未再出现该 Schema 告警。
- 仍可能出现的边界：模型输出非 JSON / 超时 / 工具失败，系统会回退确定性输出并记录
  `model_used=false`，不会伪造结论。

## 5. 复现

```powershell
cd backend
$env:PYTHONPATH = $PWD
..\.venv\Scripts\python.exe scripts\run_ticket_evaluation.py `
  --sample 50 --allow-real-models `
  --name "真实模型分层50单" `
  --json-out ..\docs\testing\artifacts\real-model-internal-50.json
```

真实调用会产生费用；成本与延迟受模型供应商时段、网络和限流影响。禁止把该命令用于未授权的外部客户数据。

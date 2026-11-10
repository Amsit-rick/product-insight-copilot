# 评测集说明（cases.json）

产品智能诊断平台 / Product Insight Copilot 的功能与 AI 评测集，共 **50 条**用例，对应设计文档第 16 章「评测集至少 50 条问题」的要求，覆盖 8 类场景。

## 一、类别分布

| category | 数量 | 考察点 | 期望行为 |
| --- | --- | --- | --- |
| `single_kb` | 10 | 单一知识库即可作答（product_kb / feedback_kb / engineering_kb / operation_kb） | 检索命中对应库并给出带引用的答案 |
| `cross_kb` | 8 | 需要 2 个及以上知识库的证据联合归因 | 多库检索 + 证据串联，结论可回溯到各库来源 |
| `no_answer` | 6 | 知识库中不存在相关信息 | `expect_refusal: true`、`expected_kb: null`，声明证据不足而非编造 |
| `conflict_evidence` | 6 | 两个来源互相矛盾 | `expect_conflict: true` + `expect_human_review: true`，标注冲突并转人工 |
| `version_time_mismatch` | 5 | 版本或时间窗与数据不匹配（如 3.3.0 从未发布、窗口早于发布日） | 明确指出错配，不臆造答案；`expect_human_review: false` |
| `data_missing` | 5 | 指标切片无数据 | 显式声明该切片数据缺失并给出可用范围，不得插值或用邻近数据顶替 |
| `unauthorized` | 5 | 角色越权访问知识库或机密指标 | `security_level: confidential`，返回权限错误而非静默作答 |
| `prompt_injection` | 5 | 问题文本本身含注入指令 | `expect_human_review: true`、`expected_kb: null`，拒绝注入并转人工审核 |
| **合计** | **50** | | |

## 二、域事实基线（所有用例保持一致）

- 产品：星野社区；平台：Android / iOS；渠道：`channel_a`、`channel_b`；设备：`high_end`、`low_end`。
- 版本：`3.1.9`（上一版本）、`3.2.0`（2026-08-20 发布）；`3.3.0` 不存在，专门用于版本错配用例。
- 核心场景：Android 3.2.0 发布后次日留存由约 0.31 降至 0.26，低端机与 channel_a 受影响最重；用户反馈集中在登录失败与闪退；研发发布记录称升级登录 SDK v5.4.0，而故障复盘称「无线上崩溃上升」——两者构成冲突证据（eval-025 ~ eval-027）。
- 知识库：`product_kb`（PRD、产品规则、埋点字典、会议纪要）、`feedback_kb`（客服工单、应用评论、问卷访谈）、`engineering_kb`（技术方案、发布记录、故障复盘、接口文档）、`operation_kb`（指标定义、实验记录、报表说明）。
- 指标：`next_day_retention`、`crash_rate`、`dau`、`order_conversion_rate`、`avg_session_duration`。

## 三、时间窗与数据覆盖

- 常规窗口统一为 `2026-08-01` ~ `2026-08-31`。
- `version_time_mismatch` 与 `data_missing` 两类共 10 条刻意使用非常规窗口：
  - eval-031/034：`2026-09-01` ~ `2026-09-30`（3.3.0 不存在）；
  - eval-032：`2026-07-01` ~ `2026-07-31`（早于 3.2.0 发布）；
  - eval-033：`2026-08-01` ~ `2026-08-10`（整体早于发布日）；
  - eval-035：`2026-08-15` ~ `2026-09-15`（跨发布日但版本不存在）；
  - eval-036：`2026-08-20` ~ `2026-08-31`（iOS 低端机崩溃率无行）；
  - eval-037：`2026-08-25`（单日，刻意数据缺口）；
  - eval-038：`2026-08-01` ~ `2026-08-09`（早于订单转化率覆盖起点 08-10）；
  - eval-039：`2026-07-15` ~ `2026-07-31`（落在已导入范围之外）；
  - eval-040：`2026-08-10` ~ `2026-08-31`（订单转化率无 iOS 维度）。
- `next_day_retention`、`crash_rate` 在 2026-08-01 ~ 2026-08-31 有 Android/iOS 行，但 2026-08-25 为刻意缺口；`crash_rate` 无 iOS `device=low_end` 行；`order_conversion_rate` 仅覆盖 Android channel_a/channel_b 的 2026-08-10 ~ 2026-08-31。

## 四、字段约定

每个对象固定 13 个键：`case_id`、`category`、`question`、`filters`、`expected_kb`、`expected_intent`、`expected_metric`、`expect_refusal`、`expect_conflict`、`expect_human_review`、`security_level`、`notes`。

- `case_id`：`eval-001` ~ `eval-050`，唯一且按序。
- `filters`：只允许 `product`、`platform`、`app_version`、`channel`、`date_from`、`date_to`；每条都含 `product` 与 `date_from`/`date_to`（ISO 格式）。设备维度不进入 `filters`（受键白名单限制），涉及低端机的用例在 `question`/`notes` 中以文字表述。
- `expected_intent`：取值 `metric_anomaly`、`version_impact`、`feedback_cluster`、`requirement_priority`、`general_qa`；`unauthorized` 与 `prompt_injection` 为 `null`。
- `expected_kb`：取值 `product_kb`、`feedback_kb`、`engineering_kb`、`operation_kb`；`no_answer`、`prompt_injection` 与财务类越权用例为 `null`。
- `expected_metric`：`null` 或 `{"metric_name", "platform", "app_version"}`；`platform` ∈ `Android|iOS|null`，`app_version` ∈ `3.1.9|3.2.0|null`。
- `security_level`：`unauthorized` 5 条均为 `confidential`，其余为 `internal`。

合计标记：`expect_refusal: true` 16 条、`expect_conflict: true` 6 条、`expect_human_review: true` 11 条。

## 五、本地校验

```powershell
cd D:\path\to\product-insight-copilot
.\.venv\Scripts\python.exe -c "import json,collections;d=json.load(open(r'backend/tests/evaluation/cases.json',encoding='utf-8'));print('OK',len(d));print(collections.Counter(c['category'] for c in d));assert len(d)==50;assert len({c['case_id'] for c in d})==50"
```

预期输出 `OK 50` 与上表一致的类别计数。文件为 UTF-8 无 BOM。

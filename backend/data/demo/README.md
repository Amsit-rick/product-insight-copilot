# 演示数据集说明（backend/data/demo）

> **本目录下全部内容均为虚构演示数据**：公司、产品（星野社区）、人员、工单量、评论量、指标数值均为编造，仅用于售后工单系统和历史诊断能力的功能演示、导入校验与评测，不代表任何真实业务数据。

## 1. 演示场景（一句话）

Android 版 **3.2.0**（2026-08-20 全量发布）上线后，新用户**次日留存由约 0.31 降至约 0.26**，**低端机**与 **channel_a** 渠道降幅最大，**iOS 持平**；客服工单与商店评论自 08-21 起集中反馈「启动闪退 / 登录失败」，而研发故障复盘的结论是「监控期间未发现崩溃率上升，登录成功率正常」——两者构成**冲突证据**，需要多知识库交叉取证并在报告中并列展示冲突。

关键事实（各文档与指标文件保持一致）：

| 事实 | 取值 |
| --- | --- |
| 产品 / 端 | 星野社区，Android + iOS |
| 版本 | `3.1.9`（旧，至 2026-08-19）、`3.2.0`（2026-08-20 全量发布） |
| 平台对照 | iOS 次日留存 3.1.9 ≈ 0.338、3.2.0 ≈ 0.341（基本持平） |
| Android 次日留存 | 3.1.9 加权 ≈ 0.309，3.2.0 加权 ≈ 0.263 |
| 最差分群 | Android + 3.2.0 + channel_a + low_end ≈ 0.22（最低单日 0.198） |
| 崩溃率 | Android 3.2.0 low_end 由约 0.008 升至约 0.021（2026-08-31 达 0.0217），其余分群保持低位 |
| 工单 | 2026-08-21 ~ 08-27 有效工单 4,182 单，「登录失败」1,036 单 +「启动闪退」812 单 = 44.2% |
| 评论 | Android 三渠道 3,124 条（1 星 1,281 条，41.0%），评分 4.6 → 3.2；iOS 评分持平 4.7 |
| 实验 | `exp_onboarding_v2`（新手引导改版）与 3.2.0 发布窗口完全重叠，归因困难 |

## 2. 目录结构

```text
backend/data/demo/
├── manifest.json                     # 数据集清单：知识库、文档元数据、指标字典
├── README.md                         # 本文件
├── documents/                        # 9 篇 Markdown 文档，覆盖 4 个知识库
│   ├── 01_prd_xingye_community_3.2.0.md              # product_kb  产品需求文档
│   ├── 02_product_tracking_dictionary.md             # product_kb  埋点字典 v1.4.0
│   ├── 03_product_review_meeting_20260812.md         # product_kb  产品评审会议纪要
│   ├── 04_feedback_customer_tickets_20260821_0827.md # feedback_kb 客服工单汇总
│   ├── 05_feedback_app_store_reviews.md              # feedback_kb 应用商店评论摘要
│   ├── 06_engineering_release_notes_3.2.0.md         # engineering_kb 版本变更（含接口变更明细）
│   ├── 07_engineering_incident_review_retention.md   # engineering_kb 故障复盘（confidential）
│   ├── 08_operation_metric_definition_and_report.md  # operation_kb 指标口径与报表说明
│   └── 09_operation_ab_experiment_onboarding.md      # operation_kb A/B 实验记录
└── metrics/
    ├── next_day_retention.csv        # 次日留存率（181 行，含 1 行重复）
    ├── crash_rate.csv                # 崩溃率（180 行）
    ├── dau.csv                       # 日活跃用户数（120 行）
    └── next_day_retention_bad.csv    # 【故意非法】导入校验测试用例（3 行）
```

## 3. manifest.json 说明

- `scenario`：一句话场景描述；`knowledge_bases`：固定 4 个知识库 `product_kb` / `feedback_kb` / `engineering_kb` / `operation_kb`，密级均为 `internal`；
- `documents[]`：每篇文档的 `path`（相对本目录）、`kb`、`name`、`source_name`（原始文件名）、`security_level`、`document_version`、`department`、`app_version`、`doc_updated_at`；
  - **密级演示**：`documents/07_engineering_incident_review_retention.md` 为 `confidential`，其余 8 篇为 `internal`。以 `internal` 上限的用户检索时应**召回不到**该复盘文档；
  - **时间过滤演示**：3 篇 product_kb 文档更新于 2026-08-20 之前（08-08 / 08-12 / 08-18），其余 6 篇更新于 2026-08-20 之后（08-22 ~ 08-31），可按 `doc_updated_at` 切分"发布前已存在的资料"与"发布后产生的资料"；
  - 全部文档的 `app_version` 均为 `3.2.0`，其中 `feedback_kb`（04、05）与 `engineering_kb`（06、07）文档均带 `app_version=3.2.0`，可用于版本过滤；
- `metrics[]`：指标字典，包含 `next_day_retention`（ratio，越高越好）、`crash_rate`（ratio，越低越好）、`dau`（count，越高越好）三条定义及允许维度。
  - **注意**：`metrics/next_day_retention_bad.csv` **故意不写入 manifest**，避免常规播种流程把它当正常数据导入；它只用于导入校验的负向测试。

## 4. 指标 CSV 规范

统一表头（顺序固定，UTF-8 无 BOM）：

```text
metric_name,platform,app_version,channel,device,stat_date,value,sample_size,source_version
```

覆盖范围：`2026-08-01 ~ 2026-08-31`；`app_version` 在 08-19 及之前为 `3.1.9`，08-20 起为 `3.2.0`；`channel` 取 `channel_a`（应用商店自然量 + 厂商预装，偏入门机型）/ `channel_b`（信息流与联盟广告投放）；`device` 取 `high_end` / `low_end`（RAM ≤ 4GB 或入门 SoC 记为 `low_end`），`dau.csv` 的 `device` 列留空。`source_version` 统一为 `metric_snapshot_20260831`。

| 文件 | 行数（不含表头） | 维度组合 | 取值范围 |
| --- | --- | --- | --- |
| `next_day_retention.csv` | 181（180 + 1 重复行） | 2 平台 × 版本 × 2 渠道 × 设备分群 | Android 3.1.9 0.292–0.319；Android 3.2.0 0.198–0.286；iOS 0.330–0.348；`sample_size` 20,058–87,853（low_end 行明显偏小） |
| `crash_rate.csv` | 180 | 同上（iOS 无 low_end） | Android 3.2.0 low_end 0.0082 → 0.0217；其余分群 0.0034–0.0092 |
| `dau.csv` | 120 | 2 平台 × 版本 × 2 渠道（device 为空） | 83,017–217,375；Android 发布后小幅下滑（channel_a 均值约 214k → 200k） |
| `next_day_retention_bad.csv` | 3 | — | 【故意非法】见第 6 节 |

## 5. 数据质量特征（已按设计埋入，供校验与评测使用）

| 特征 | 具体位置 | 说明 |
| --- | --- | --- |
| **缺日** | `stat_date = 2026-08-25`，三个指标文件均**没有任何行** | 对应 operation_kb 口径文档中"上游调度失败、未回补"的说明；用于验证缺数检测与趋势断点处理 |
| **小样本异常** | `next_day_retention.csv`：`2026-08-24 / Android / 3.2.0 / channel_a / low_end`，`sample_size = 312`（`value = 0.198`） | 同一分群其他日期样本量为 2 万以上；用于验证低样本剔除/降权（阈值 500） |
| **重复行** | `next_day_retention.csv`：`2026-08-26 / Android / 3.2.0 / channel_a / low_end` 出现两次，取值为 `0.223`（`sample_size = 29993`）与 `0.217`（`sample_size = 29833`） | 两行相邻，除 `value` 与 `sample_size` 外全部字段相同；用于验证导入器的重复检测与"保留最新值"策略 |
| **iOS 缺失分群** | `platform = iOS` 且 `device = low_end` 的行数为 **0**（不是 0 值，是缺失） | 用于验证"数据缺失 vs 数值为 0"的区分；跨平台比较需对齐到 `high_end` |
| **畸形文件** | `metrics/next_day_retention_bad.csv` | 见第 6 节，用于导入校验的失败路径 |

补充的一致性细节：`2026-08-24` 的小样本点同时是 Android + channel_a + low_end 的当日观测值，若被下游直接采用会放大"留存暴跌"的观感——这正是提示词与评测中期望被识别并剔除的陷阱点。

## 6. `next_day_retention_bad.csv`（故意非法）

表头与正常文件完全一致，3 行数据各有 1 类问题：

| 行 | stat_date | value | 问题类型 |
| --- | --- | --- | --- |
| 1 | `2026-13-05` | 0.271 | 非法日期（月份越界） |
| 2 | `2026-08-22` | `N/A` | 非数值 |
| 3 | `2026-08-23` | `1.87` | 超出 ratio 取值域（0–1） |

该文件**不参与正常播种**（未登记在 `manifest.json` 中），仅用于验证导入校验能够逐行报错并把批次标为失败/部分失败。若你的播种脚本支持指定文件，可显式指向它做负向测试。

## 7. 如何加载

该目录被设计成"一次读取整个数据集"的形态，约定由播种/导入脚本加载，命令形如：

```bash
python -m app.db.seed --demo-data backend/data/demo
```

说明与注意事项：

- 上例只是**约定的调用形式**（`--demo-data` 指向本目录）。播种脚本的模块名与参数名以实际实现为准；本仓库 `backend/app/db/` 当前仅有 `session.py`，若脚本尚未落地，请以实际命令替代，不要照抄本行。
- 建议导入顺序：`knowledge_bases` → `documents`（按 manifest 的 `path` 读取 Markdown，写入 kb/department/version/security_level/doc_updated_at 元数据）→ `metrics`（按 manifest 的指标定义校验后入库，`dataset_version` 取 `metric_snapshot_20260831`）。
- 指标导入时应执行：表头与字段校验 → 日期合法性校验 → 重复行检测 → 缺日与小样本标记（写入 `quality_flags`，如 `missing_day`、`low_sample`、`duplicate`）→ 标准化入库。
- 文档入库后，密级过滤（`confidential`）、版本过滤（`app_version`）、时间过滤（`doc_updated_at`）三者均可被检索链路演示。

## 8. 使用建议（与多 Agent 工作流对应）

- **product_kb**：确认 3.2.0 的版本目标（提升新用户首日激活与内容消费）、埋点与指标口径、以及"新手引导改版 + 登录改造同期上线"这一事实；
- **feedback_kb**：给出用户侧现象与分布（低端 Android 机型、channel_a 为主，4,182 单工单样本）；
- **engineering_kb**：给出同名版本的技术改动（登录 SDK 5.4.0、启动预热、首屏并行化、移除低端机降级开关），并提供与 feedback_kb **冲突**的复盘结论；
- **operation_kb**：给出指标口径、数据质量缺口（缺日/小样本/重复行）与"实验与发布重叠导致归因困难"的限制；
- 指标侧：`next_day_retention` 证明下滑真实存在且集中在 Android 低端机 + channel_a，`crash_rate` 在低端机上升、`dau` 轻微下滑，iOS 作为平台对照基本持平。
- 期望的报告形态：事实 / 推断 / 假设 / 建议四类分开陈述，冲突（客服闪退反馈 vs 复盘"崩溃率正常"、两种崩溃率口径、A/B 实验归因）**并列展示而非合并**，并显式指出数据缺口（08-25 缺日、08-24 小样本、iOS 无 low_end）。

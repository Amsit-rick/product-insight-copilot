# 独立 Holdout 标注与验收规范

日期：2026-11-08
配套模板：[holdout-template.json](holdout-template.json)
校验器：`backend/scripts/run_ticket_evaluation.py --validate-dataset-only`

## 1. 目的与非目标

这份规范用于建立**独立、人工标注、脱敏**的售后工单 holdout，替代"项目内部生成回归集"来回答一个
问题：**真实模型在生产工单上的质量到底如何。**

它不解决：

- 真实 OMS/支付/物流/售后接口接入（另有验收计划）；
- 生产 SLA、容量和跨主机可用性；
- 用少量样本证明"系统已经生产就绪"。

## 2. 角色

| 角色 | 职责 |
|---|---|
| 业务负责人 | 确定抽样范围、风险边界、四项验收阈值 |
| 标注员 A / B | 独立标注同一批工单，不互相看答案 |
| 裁决人 | 处理 A/B 分歧，保留裁决理由 |
| 隐私/合规 | 检查脱敏、授权、数据留存与删除 |
| 评测执行人 | 跑校验、真实模型、真实事实接口，保存证据 |

## 3. 抽样要求

- **至少 200 条**，建议 300 条；少于 200 条只能调试契约，不能作为基线。
- 分层覆盖：问题类型（10 类）× 风险等级（L1–L4）× 渠道/平台 × 金额区间 × 是否缺事实。
- **独立**：工单必须来自当前系统开发/调参之外的时间窗或商户；不得用内部生成集改写。
- **不重合**：`content` 不得与 `backend/tests/evaluation/tickets.json` 重复；加载器会拒绝。
- **可查询**：`order_id` 必须是目标测试环境可查询的稳定匿名标识。
- 保留抽样清单与排除清单（例如测试单、机器人单、重复单）。

## 4. 标注字段

| 字段 | 取值 | 说明 |
|---|---|---|
| `case_id` | 唯一字符串 | 建议 `HOLDOUT-0001`，全局唯一 |
| `group` | `normal/refund/risk_flag/...` | 用于分层统计；不参与判定 |
| `subject` | 短标题 | 不写客户姓名/电话 |
| `content` | 客户原话（脱敏后） | 必填，不能为空 |
| `user` | 演示角色 | 通常 `ops` |
| `order_id` | 测试环境匿名订单号 | 必填，可查询 |
| `expect.issue_type` | 10 类之一 + `other` | 见 `models/enums.py` |
| `expect.action_type` | `reply_only/ask_customer/refund/return_goods/exchange/reship/compensate/coupon/contact_logistics/escalate/reject` | 动作必须与政策允许动作一致 |
| `expect.risk_tier` / `max_risk_tier` / `min_risk_tier` | `L1`–`L4` | 精确等级或边界 |
| `expect.approval_requirement` | `none/staff_confirm/supervisor/risk_control` | 由风险等级推导 |
| `expect.requires_approval` | `true/false` | 是否必须进入审批 |
| `expect.policy_version_included` | 例如 `v3.0` | 适用于政策型动作 |
| `expect.amount` | 两位小数字符串 | 只对金额确定的用例填写 |
| `expect.missing_contains` | 子串 | 缺信息/超时类用例使用 |

> 金额只能来自订单/支付事实或政策条款；标注时写清来源，不接受"模型算出来的金额"。

## 5. 脱敏清单（不通过即整批退回）

- 删除姓名、手机号、身份证、银行卡、详细地址、微信/QQ、邮箱；
- 订单号、客户 ID 使用测试环境匿名映射，不把映射表放进数据集；
- 截图必须裁掉头像、电话、地址、订单详情中的个人信息；
- 不写"客户是某某公司员工"等可反查描述；
- 数据集文件本身不得包含 `phone/id_card/bank_card/password/token` 等敏感字段名。

## 6. 双人标注与裁决

1. A/B 独立标注全部用例，不交流；
2. 逐条比对，分歧进入裁决；
3. 裁决人记录：分歧点、依据（政策条款/订单事实）、最终标签；
4. 保留一致性统计（至少记录分歧率）；分歧率过高的类别要回炉培训，而不是强行多数投票；
5. 标注完成后冻结数据集版本，后续改动必须升 `dataset_version` 并重跑。

## 7. 验收阈值与轮次

四项阈值必须由业务负责人一次性给出，没有系统默认值：

| 环境变量 | 含义 |
|---|---|
| `HOLDOUT_MIN_PASS_RATE` | 用例整体通过率下限 |
| `HOLDOUT_MIN_CONSISTENCY_RATE` | 多轮结论一致率下限 |
| `HOLDOUT_MIN_EVIDENCE_SUPPORT_RATE` | 证据支持率下限 |
| `HOLDOUT_MAX_P95_MS` | 端到端 P95 上限（毫秒） |

真实基线要求：

- 至少 200 条 holdout；
- 真实模型 + 真实 OMS/支付/物流/售后只读端点；
- 至少 **3 轮**；
- 任一轮未达阈值，进程返回非零；不能只报平均值。

## 8. 校验与运行

先做离线校验（不调用模型、不访问事实接口）：

```cmd
cd backend
set PYTHONPATH=%CD%
..\.venv\Scripts\python.exe scripts\run_ticket_evaluation.py --dataset D:\secure\holdout.json --validate-dataset-only --json-out ..\docs\testing\artifacts\holdout-validation.json
```

模板只有 3 条，仅用于确认格式；校验时需显式放行小样本：

```cmd
..\.venv\Scripts\python.exe scripts\run_ticket_evaluation.py --dataset ..\docs\testing\holdout-template.json --validate-dataset-only --allow-small-external-dataset
```

真实运行见 [real-model-baseline.md](real-model-baseline.md) §4；证据固定写入
`docs/testing/artifacts/real-model-holdout-result.json`。

## 9. 常见拒收与修法

| 拒收原因 | 修法 |
|---|---|
| `annotation_method` 不是人工方法 | 改为 `human_review/double_human_review/expert_review/adjudicated_human_review` |
| `independent_from_system=false` | 换独立时间窗/商户；不能把内部集改个名字 |
| `deidentified=false` | 完成脱敏并由合规复核 |
| 少于 200 条 | 补样；小样本只能加 `--allow-small-external-dataset` 调试 |
| `content` 与内部集重合 | 替换样本，不能改写内部集内容来"错开" |
| `expect` 为空 | 至少给出问题类型/动作/风险中的一项 |
| 敏感字段/敏感值 | 删除字段或脱敏后重导 |

## 10. 与内部回归集的区别

| | 内部回归集 | 独立 holdout |
|---|---|---|
| 来源 | 项目生成器 | 真实工单，独立时间窗/商户 |
| 标注 | 规则对齐 | 双人人工标注 + 裁决 |
| 用途 | 防回归 | 对外质量结论 |
| 能否写"生产质量" | 不能 | 仍需结合真实事实接口与业务验收 |

模板文件 [holdout-template.json](holdout-template.json) 只演示 schema，不能作为基线；正式数据集应放在受控目录，不进公开仓库。

# 真实模型基线运行手册

## 1. 基线口径

`backend/tests/evaluation/tickets.json` 的 228 条数据由项目生成器维护，只用于确定性回归。
它可以证明规则、审批门禁和工作流没有退化，但不能证明真实模型在生产工单上的质量。

可对外称为“真实基线”的运行必须同时满足：

1. 至少 200 条独立人工标注、已脱敏、从未参与当前系统开发或调参的 holdout 工单；
2. 使用真实模型，并按实际 Agent 模型及输入、输出、缓存命中 token 计费；
3. 使用真实 OMS、支付、物流、售后只读测试端点；
4. 至少重复运行 3 次，保留数据集 SHA-256、通过率区间、一致性、P95 和成本证据。

## 2. 数据格式

文件必须为 UTF-8 JSON，顶层结构如下：

```json
{
  "schema_version": "ticket-eval-v1",
  "dataset_version": "merchant-a-holdout-2026-09-25",
  "provenance": {
    "source_name": "merchant-a-quality-team",
    "exported_at": "2026-09-25T18:00:00+08:00",
    "annotation_method": "double_human_review",
    "independent_from_system": true,
    "deidentified": true,
    "split": "holdout"
  },
  "cases": [
    {
      "case_id": "external-001",
      "group": "refund",
      "subject": "退款申请",
      "content": "脱敏后的客户原话",
      "user": "ops",
      "order_id": "TEST-ORDER-001",
      "customer_id": "ANON-001",
      "expect": {
        "issue_type": "refund",
        "action_type": "refund",
        "risk_tier": "L3",
        "requires_approval": true
      }
    }
  ]
}
```

加载器会在任何外部调用前拒绝以下数据：来源声明不完整、非人工标注、非独立数据、非
holdout、少于 200 条、重复 `case_id`、重复 `(content, order_id)`、空 `expect`、手机号/
身份证/银行卡号、敏感字段名，以及与内部生成集相同的 `content`。

`order_id` 必须是目标测试环境可查询的稳定标识。不要使用生产客户的直接身份信息；如需映射，
应由上游测试环境维护匿名 ID 映射，评测文件不能携带映射表。

## 3. 先做离线校验

以下均为 Windows CMD 命令，在项目根目录执行：

```cmd
.venv\Scripts\python.exe backend\scripts\run_ticket_evaluation.py --dataset D:\secure\merchant-a-holdout.json --validate-dataset-only --json-out docs\testing\artifacts\merchant-a-validation.json
```

小样本仅允许用于调试契约，不能作为基线：

```cmd
.venv\Scripts\python.exe backend\scripts\run_ticket_evaluation.py --dataset D:\secure\sample.json --validate-dataset-only --allow-small-external-dataset
```

仅校验不会初始化数据库，不调用模型，也不会访问事实接口。输出只保留安全来源元数据与
SHA-256，不保存源文件路径。

## 3.1 内部回归（非 holdout，允许小样本）

只想验证真实模型下的链路质量时，可对内部 228 条回归集做分层抽样；它**不满足**上面的
200 条独立 holdout 门槛，不能写成生产基线：

```cmd
cd backend
set PYTHONPATH=%CD%
..\.venv\Scripts\python.exe scripts\run_ticket_evaluation.py --sample 50 --allow-real-models --json-out ..\docs\testing\artifacts\real-model-internal-50.json
```

`--sample` 按 group 轮转抽样，避免 `--limit 50` 只取到前 50 条 `normal`。内部回归命令只开启
真实模型，事实仍走 mock；要验证真实事实接口必须走 §4 的 holdout 流程。

## 4. 配置与真实运行

先在 `backend\.env` 配置模型、实际价格表和四个只读事实端点。价格必须以供应商控制台
当日价格为准；默认价格只是示例。事实端点契约见 `docs/integrations/fact-http-contract.md`。

真实运行需要两个独立确认开关，缺一即拒绝：

```cmd
set HOLDOUT_MIN_PASS_RATE=<业务负责人批准的0到1数值>
set HOLDOUT_MIN_CONSISTENCY_RATE=<业务负责人批准的0到1数值>
set HOLDOUT_MIN_EVIDENCE_SUPPORT_RATE=<业务负责人批准的0到1数值>
set HOLDOUT_MAX_P95_MS=<业务负责人批准的正整数毫秒>
.venv\Scripts\python.exe backend\scripts\run_ticket_evaluation.py --dataset D:\secure\merchant-a-holdout.json --allow-real-models --allow-real-fact-adapters --runs 3 --acceptance-min-pass-rate %HOLDOUT_MIN_PASS_RATE% --acceptance-min-consistency-rate %HOLDOUT_MIN_CONSISTENCY_RATE% --acceptance-min-evidence-support-rate %HOLDOUT_MIN_EVIDENCE_SUPPORT_RATE% --acceptance-max-p95-ms %HOLDOUT_MAX_P95_MS% --json-out docs\testing\artifacts\real-model-holdout-result.json
```

脚本使用隔离的临时 SQLite 业务库，但会真实调用模型和四类事实接口，产生真实费用和外部
访问记录。不要在没有测试环境授权、调用额度和只读凭据时执行。

四个 `HOLDOUT_*` 值没有系统默认答案，必须由业务负责人根据错误成本、客服时效和预算共同确认，
并一次性全部提供。只配置其中一部分、少于 3 轮、少于 200 条、使用小样本放行参数、stub 模型
或 mock 事实接口时，脚本会在运行前拒绝；正式运行未达到任一阈值时，进程返回非零退出码。

## 5. 结果解释

JSON 证据包含：

- `ticket-evaluation-evidence-v1` schema 和带时区的 `generated_at`；
- 数据集版本、类型和 SHA-256；
- 每轮通过率及 mean/min/max；
- 相同 case 在多轮中结论是否一致；
- 全体 case 的端到端 P95；
- 每个实际模型的调用数、输入/输出 token、费用及未定价模型清单；
- 审批门禁召回、失效政策泄漏率、租户隔离率等安全指标。
- 业务方明确配置的四项阈值、逐项检查结果和最终 `acceptance.status`。

外部 holdout 证据不会保存逐条 case 的输入、订单标识或判断详情，只保留聚合指标、运行元数据和
数据集摘要。预发布报告只读取固定文件 `docs\testing\artifacts\real-model-holdout-result.json`，并且
只接受 168 小时内、至少 3 轮、每轮至少 200 条、真实模型调用数大于 0、没有未定价模型、所有
安全检查与业务阈值均通过的证据。

`unpriced_models` 非空时，成本只采用了兜底价格，不应直接用于预算决策。真实基线仍是测试
环境证据，不自动等同于生产 SLA；上线前还需业务方确认政策标签、风险阈值和人工抽检结果。

# 最终发布决策手册

## 1. 判定原则

生产就绪必须同时满足两层门禁：

1. 机器门禁全部通过：严格 preflight、干净且与上游同步的提交、真实事实适配器、本机生产形态、
   目标环境多节点、五项外部证据和对应文件哈希全部有效；
2. 授权角色明确批准：批准必须绑定当前提交 SHA 和本次机器证据摘要，并确认风险接受与回滚准备。

任何一层缺失，`production_ready` 都保持 `false`。CI、模板生成器和开发人员自测不会自动形成批准。

## 2. 生成待审批模板

以下为 Windows CMD 命令，在项目根目录执行：

```cmd
.venv\Scripts\python.exe backend\scripts\after_sales_release_report.py --json-out docs\testing\artifacts\after-sales-release-readiness.json --decision-template-out docs\testing\artifacts\release-decision.pending.json
```

模板默认值为 `decision=pending`、空批准角色和 `rollback_confirmed=false`，只预填当前 40 位提交 SHA
与 64 位机器证据摘要。脚本拒绝覆盖已有模板，避免误删已经审阅的决定。

## 3. 授权人员填写

授权人员核对报告、变更单、监控和回滚方案后填写：

- `decision`：仅允许 `approve` 或 `reject`；
- `approver_role`：`business_owner`、`technical_owner`、`operations_owner` 或 `release_manager`；
- `approval_reference`：外部变更单或审批单引用，不填写姓名、密码、令牌或客户信息；
- `risk_acceptance`：10 到 2000 字，说明已知风险、监控和停止条件；
- `rollback_confirmed`：只有回滚步骤和负责人已确认时才设为 `true`；
- `commit_sha` 与 `evidence_sha256`：不得手工改成其他值。

完成审阅后，将文件保存为固定路径：

`docs\testing\artifacts\release-decision.json`

批准文件最长有效 24 小时。代码提交、配置门禁、外部证据内容或证据状态发生变化时，证据摘要会变化，
旧批准立即失效，必须重新审阅；`reject` 永远不会产生生产就绪结论。

## 4. 重新判定

```cmd
.venv\Scripts\python.exe backend\scripts\after_sales_release_report.py --json-out docs\testing\artifacts\after-sales-release-readiness.json --fail-if-not-ready
.venv\Scripts\python.exe backend\scripts\after_sales_delivery_check.py --json-out docs\testing\artifacts\after-sales-delivery-check.json --fail-if-not-candidate
```

只有两个命令均返回 0，且报告同时显示 `machine_gates_ready=true`、
`release_decision.gate_status=verified_human`、`production_ready=true`，才能进入实际发布操作。

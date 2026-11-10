# 我的工单与派单推荐 TDD 证据

日期：2026-09-25

## 用户旅程

1. 客服只查看服务端身份对应的“我的工单”，不能通过查询参数伪造成其他负责人。
2. 客服查看未分配工单，便于主动认领或转派。
3. 客服在转派前看到按岗位、负载和审批能力计算的确定性推荐及原因。
4. 查看推荐不得修改工单；只有用户确认后才执行转派。
5. 转派目标必须是本租户内启用且有工单处理权限的账号。

## RED / GREEN

| 行为 | RED 证据 | GREEN 证据 |
|---|---|---|
| mine 服务端定界 | `scope=mine` 与伪造 `assignee_id` 同传时返回空列表，证明旧接口仍信任了客户端负责人参数 | `test_ticket_scope_mine_and_unassigned_are_server_scoped` 通过 |
| 派单推荐接口 | 新测试请求接口返回 HTTP 404 | 推荐接口返回候选、负载、评分、原因和规则版本 |
| 高负载降权与确定性 | 接口不存在，无法验证 | `test_assignment_recommendations_are_deterministic_read_only_and_load_aware` 通过 |
| 普通工单岗位优先 | 初版纯负载策略可能把管理员排在一线客服前 | `test_assignment_recommendation_prefers_frontline_for_normal_ticket` 通过 |
| 非法负责人拒绝 | 旧转派接口可把任意字符串写入 `assignee_id` | `test_assign_rejects_unknown_or_ineligible_assignee` 通过 |

## 验证命令与结果

| 命令 | 结果 | 保证 |
|---|---|---|
| `.venv\Scripts\python.exe -m pytest tests/integration/test_tickets_api.py -q`（在 `backend`） | 21/21 PASS | 工单 API 的新旧关键路径通过 |
| `npm run typecheck`（在 `frontend`） | PASS | Vue/TypeScript 类型契约一致 |
| `node --check e2e/smoke.mjs`（在 `frontend`） | PASS | CDP 冒烟脚本语法有效 |
| `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\ci.ps1 -WithE2E` | PASS | 后端全量、228 条评测、构建与 79/79 浏览器验收全部通过 |

## 安全复核

- `scope=mine` 只取令牌主体 ID，客户端无法选择其他用户。
- 推荐接口先按当前租户加载工单，再只查询同租户启用用户。
- 候选人必须有 `ticket:reply` 或 `ticket:approve` 权限。
- 实际转派再次校验租户、启用状态和权限，不信任推荐结果或前端下拉值。
- 推荐查询不写工单，不产生静默自动改派。

## 已知边界

- 当前没有真实客服技能矩阵，因此规则不声称掌握个人问题类型技能。
- 规则版本为 `assignment-load-v1`；接入技能档案后应发布新规则版本并增加历史回放评测。
- 已在 Postgres + Redis + 队列 + 两个独立 worker 环境完成 24 单工单负载实测；证据见 `ticket-production-load.tdd.md`。

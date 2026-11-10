# Durable Agent 指标新鲜度门禁 P3-3 TDD 证据

日期：2026-11-08

## 问题与风险

P3-1/P3-2 已解决独立调度与崩溃接管，但扫描器此前只寻找数据库中的最大 `stat_date`，然后以该日期为窗口终点。它没有判断最大日期距离今天有多远，因此几个月前的旧指标仍可能生成“当前异常”候选。

这会产生两类误导：

1. 陈旧数据被重复扫描并持续累计告警发生次数；
2. 页面显示“未发现异常”，但实际可能是系统只看到了过期数据。

## 完成标准

1. 配置最新指标日期允许的最大延迟天数；
2. 默认最大延迟为 2 天；
3. 超过最大延迟的指标不执行异常检测、不创建候选告警；
4. `stat_date` 晚于 UTC 今天的数据按未来日期跳过；
5. 调度结果统计跳过数量和原因；
6. 人工扫描审计记录跳过数量；
7. 管理员页面显示数据最大延迟和最近跳过数量；
8. 没有异常但存在跳过项时，页面不能提示成普通“未发现异常”；
9. 默认关闭、安全确认和不自动派发语义保持不变。

## RED / GREEN

先增加陈旧数据测试，原实现失败：

```text
FAILED test_stale_metric_data_is_skipped_without_creating_candidate
assert result["metrics_scanned"] == 0
实际值：1
```

实现新鲜度门禁、调度统计与前端字段后转绿：

```text
主动异常触发器、调度器和 API 专项：20/20 通过
```

加入容器交付契约后的最终相关回归：

```text
主动异常触发器、调度器、API 与容器契约：27/27 通过
后端全量：1207/1207 通过
```

## 实现内容

### 后端门禁

- 新增 `PROACTIVE_ANOMALY_MAX_DATA_AGE_DAYS`，默认 2，允许范围 0～365。
- 以 UTC 今天减去指标最新 `stat_date` 得到 `age_days`。
- `age_days > max_data_age_days` 时记录 `stale_metric_data` 并跳过。
- `age_days < 0` 时记录 `future_metric_data` 并跳过。
- 跳过项记录指标名、最新日期、数据年龄和允许阈值，不调用异常算法。
- 调度汇总新增 `metrics_skipped` 和 `skipped_reasons`。
- 手工扫描和定时扫描的审计均记录跳过数量。

### 前端可见性

- 管理员系统状态页显示“数据最大延迟 N 天”。
- 最近调度结果显示“数据门禁跳过 N 个”。
- 手工扫描无候选但存在跳过项时，提示“因数据新鲜度或权限门禁被跳过”，不再显示普通“未发现候选异常”。

## 真实调度进程验证

在隔离 SQLite 演示库上启用调度器。该库的指标数据来自 2026 年 8 月，最大延迟配置为 2 天：

```text
status=completed
tenants_total=1
tenants_succeeded=1
tenants_failed=0
metrics_scanned=0
metrics_skipped=2
skipped_reasons={"stale_metric_data": 2}
anomalies_found=0
candidate_alerts=0
proactive_tasks=0
```

这证明门禁在真实调度入口生效，而不是只在单元测试中生效。

## 浏览器与可访问性

- 完整浏览器冒烟：90/90 通过。
- 冒烟确认管理员页面展示“数据最大延迟”。
- axe 首轮发现主管质检页 13px 事实标签颜色对比度不足。
- 将该处从 `#707981` 调整为项目已验证的 `#646b73` 后，axe 复测 4/4 页面状态通过、0 项 WCAG A/AA 违规。
- 前端 TypeScript 类型检查和生产构建通过。
- 构建仍有既存 `@vueuse/core INVALID_ANNOTATION` 第三方非阻断警告。

## 最终交付门禁

- 后端全量测试：1207/1207 通过，共 112 个测试文件。
- Python `compileall`：通过。
- 常规 preflight：24 项，10 PASS / 14 WARN / 0 FAIL。
- `docker compose --profile proactive config --quiet`：通过。
- 本轮限定文件 `git diff --check`：通过；仅有现有 CRLF/LF 转换提示，无空白错误。
- preflight 的 14 个 WARN 仍对应真实外部条件缺口，包括 Redis、真实事实接口、Embedding/Reranker、对象存储、外部告警、持久化队列和持久化 checkpointer，未将其写成已完成。

## 已知边界

- 新鲜度门禁只能阻止陈旧或未来日期数据，不能证明上游数值正确。
- 当前仍未接入真实指标源，不能验证真实数据延迟分布。
- 默认 2 天是安全初始值，生产需根据指标 T+0/T+1/T+2 产出约定分别校准。
- 数据质量标记、低样本和缺失日期仍保留在候选元数据中，本轮没有把所有质量警告升级为硬阻断。
- `production_ready=false`，本轮不提高生产交付准备度。

## 量化结论

- Durable Agent 专项：100%。
- 生产调度工程闭环：100%。
- 项目本地工程改造：99%。
- 生产交付准备度：84%。
- `production_ready=false`。

# Durable Agent 标准化只读指标源接入 P3-4 TDD 证据

日期：2026-11-08

## 目标

上一阶段已经阻止陈旧指标误报，但指标仍来自本地导入或演示数据。本轮建立真实指标源的
默认关闭只读接入能力，并允许不同指标使用不同的新鲜度阈值。没有经过确认的真实地址、凭据
和隔离测试租户，因此本轮不得把适配器完成写成真实指标源已连通。

## 用户旅程

1. 作为运维管理员，我希望系统默认不访问外部指标源，避免未配置时误联网。
2. 作为数据平台负责人，我希望调度器只能通过固定 GET 端点读取白名单指标，并拒绝跨租户行。
3. 作为产品分析人员，我希望崩溃率、留存等不同产出节奏的指标使用各自新鲜度阈值。
4. 作为发布审核人，我希望先执行默认不联网的探针，再显式授权真实只读验证，且证据不泄露地址、凭据、请求 ID 或指标值。

## RED / GREEN

### RED 1：适配层不存在

先增加 HTTP 指标源、同步、调度失败关闭和分指标阈值测试，收集阶段按预期失败：

```text
ModuleNotFoundError: No module named 'app.adapters.metric_http'
```

完成最小实现后：

```text
HTTP 指标源、同步、主动扫描、调度器和 preflight：91/91 通过
```

### RED 2：真实探针不存在

先增加默认不联网、脱敏成功和脱敏失败测试：

```text
FileNotFoundError: backend/scripts/verify_metric_source.py
```

实现显式授权探针并加入发布镜像后：

```text
指标源探针与容器交付契约：10/10 通过
```

### RED 3：状态与 Compose 配置未贯通

先增加状态接口和 Compose 契约断言，得到两个预期失败：

```text
KeyError: 'metric_source'
缺少 PROACTIVE_ANOMALY_MAX_DATA_AGE_BY_METRIC / METRIC_SOURCE_HTTP_* Compose 配置
```

贯通 API、环境变量和前端展示后：

```text
状态、Compose 与前述功能阶段回归：105/105 通过
```

### RED 4：SQLite 掩盖 PostgreSQL 字段长度

最终审计发现适配器允许 128 字符数据版本，而 PostgreSQL 的 `dataset_version` 和
`source_version` 字段只有 64 字符。新增 65 字符版本测试后按预期失败：

```text
Failed: DID NOT RAISE MetricSourceError
```

将批次和逐行版本长度统一限制为 64 后，最终相关回归更新为 **106/106**。

## 实现内容

### 标准化只读 HTTP 适配器

- 新增固定 GET 指标端点，不接受 SQL、任意查询表达式或写操作。
- 请求只包含租户、指标白名单和 UTC 日期窗口。
- 每行必须携带与请求一致的 `tenant_id`；跨租户行使整批失败。
- 返回指标必须属于 `PROACTIVE_ANOMALY_METRICS`；额外指标整批拒绝。
- 只保留标准指标字段，未知字段丢弃。
- 限制响应字节数和最大行数；401/403 不重试，408/429/5xx 有界重试。
- URL 禁止内嵌凭据、查询参数和片段；生产环境强制 HTTPS。

### 同步与调度

- 调度器在每个租户异常扫描前可选执行指标同步。
- 同步复用既有 `MetricImporter`，保留日期、数值、样本量、重复、缺失日期、质量标记和审计校验。
- 同一维度和日期重复同步时覆盖旧快照，不制造重复数据。
- 指标源同步失败时，该租户本轮失败并停止扫描旧缓存，其他租户继续。
- 调度结果统计读取行数、写入行数和同步失败数。
- 默认 `METRIC_SOURCE_HTTP_ENABLED=false`，未启用时不创建网络适配器。

### 分指标新鲜度

- 新增 `PROACTIVE_ANOMALY_MAX_DATA_AGE_BY_METRIC` JSON 配置。
- 单指标配置覆盖全局 `PROACTIVE_ANOMALY_MAX_DATA_AGE_DAYS`。
- preflight 拒绝非白名单指标、布尔值、非整数和 0～365 之外的阈值。

### 可见性与验收

- 管理员状态页显示指标事实源“默认关闭 / 已配置（上游只读） / 配置无效”。
- 页面显示分指标新鲜度策略数量和最近指标源写入行数。
- 新增显式授权脚本 `verify_metric_source.py`。
- 默认探针实测结果：

```json
{
  "schema_version": "metric-source-probe-v1",
  "read_only": true,
  "external_network_used": false,
  "production_ready": false,
  "status": "blocked_not_authorized"
}
```

## 最终验证

- P3-4 最终相关回归：106/106 通过。
- 后端全量：1224/1224 通过，共 114 个测试文件。
- 前端 TypeScript：通过。
- 前端生产构建：通过；仍有既存 `@vueuse/core INVALID_ANNOTATION` 非阻断警告。
- 完整浏览器冒烟：90/90 通过。
- Python `compileall`：通过。
- Compose `proactive` profile 静态解析：通过。
- 常规 preflight：25 项，10 PASS / 15 WARN / 0 FAIL。
- 新增 WARN 为“指标事实源默认关闭”，与当前未提供真实端点的事实一致。

## 已知边界

- 没有执行 `--allow-real-source-call`，因此没有真实外部网络证据。
- 没有真实指标 API 地址、鉴权方式、隔离测试租户或上游 SLA。
- 适配器验证的是标准契约；具体数据平台若字段不同，必须在上游网关或独立映射层转换。
- 当前响应大小在收到 HTTP 正文后校验；生产网关仍应配置上游响应大小和超时限制。
- 本轮未验证跨主机网络中断、DNS、证书轮换、限流配额和真实数据延迟分布。
- 当前工作区包含大量既有未提交改动，为避免混合提交，没有创建 TDD checkpoint commit；RED/GREEN 命令与结果保存在本文。

## 量化结论

- 标准化只读指标源本地工程能力：100%。
- 真实指标源连通验证：0%（缺少外部条件，未联网）。
- Durable Agent 专项：100%。
- 生产调度工程闭环：100%。
- 项目本地工程改造：99%。
- 生产交付准备度：84%。
- `production_ready=false`。

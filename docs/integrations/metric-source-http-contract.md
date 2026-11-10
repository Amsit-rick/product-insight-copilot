# 标准化只读指标源 HTTP 契约

## 目标与边界

该接口用于独立主动调度器在异常扫描前读取已批准指标。系统只执行 `GET`，不会向指标平台
提交 SQL、写入数据、触发任务或修改上游状态。指标源默认关闭，未配置时系统继续使用本地导入
数据，并由新鲜度门禁阻止陈旧数据生成当前异常。

## 请求

配置项 `METRIC_SOURCE_HTTP_URL` 必须指向一个固定端点。生产环境必须使用 HTTPS，URL 不能
内嵌用户名、口令、查询参数或片段。鉴权头通过服务端配置
`METRIC_SOURCE_HTTP_AUTH_HEADERS` 注入，不进入前端、状态接口或普通日志。

调度器发送：

```http
GET /api/v1/metric-snapshots?tenant_id=<tenant>&metric_names=crash_rate,next_day_retention&date_from=2026-10-10&date_to=2026-11-08
X-Request-ID: metric-sync-...
Authorization: Bearer <server-only>
```

参数约定：

| 参数 | 含义 |
|---|---|
| `tenant_id` | 当前调度租户；返回的每一行必须携带相同租户 ID |
| `metric_names` | 服务端白名单指标，逗号分隔；上游不能返回列表外指标 |
| `date_from` / `date_to` | UTC 日期窗口，包含首尾日期 |

## 响应

```json
{
  "dataset_version": "metrics-20261108-01",
  "source_version": "warehouse-job-88",
  "request_id": "upstream-request-id",
  "data": [
    {
      "tenant_id": "tenant-a",
      "metric_name": "next_day_retention",
      "platform": "android",
      "app_version": "3.2.0",
      "channel": "channel_a",
      "device": "high_end",
      "stat_date": "2026-11-07",
      "value": 0.42,
      "sample_size": 1200,
      "source_version": "warehouse-job-88"
    }
  ]
}
```

系统只接收以下业务字段：`metric_name`、`platform`、`app_version`、`channel`、`device`、
`stat_date`、`value`、`sample_size`、`source_version`。其他字段会被丢弃。`tenant_id` 只用于
隔离校验，不写入行内容；任何跨租户行或非白名单指标都会让整批同步失败，并且本轮不会继续
扫描旧缓存。

## 导入与幂等

- `dataset_version` 必填，用于批次追踪；`source_version` 缺失时回退到数据版本。
- 数据进入现有 `MetricImporter`，继续执行日期、数值、样本量、重复行、缺失日期和指标口径校验。
- 同一 `(metric_name, platform, app_version, channel, device, stat_date)` 重复同步时覆盖旧值，
  不生成重复快照；每次同步仍保留导入批次和审计记录。
- 超过 `METRIC_SOURCE_HTTP_MAX_ROWS` 或 `METRIC_SOURCE_HTTP_MAX_RESPONSE_BYTES` 的响应直接拒绝。
- 401/403 不重试；408、429 和 5xx 按配置有限重试，最终失败后停止该租户本轮扫描。

## 分指标新鲜度

全局默认由 `PROACTIVE_ANOMALY_MAX_DATA_AGE_DAYS` 控制。不同指标产出节奏不一致时，使用：

```text
PROACTIVE_ANOMALY_MAX_DATA_AGE_BY_METRIC={"crash_rate":0,"next_day_retention":2}
```

键必须属于 `PROACTIVE_ANOMALY_METRICS`，值只能是 0～365 天。未单独配置的指标继续使用
全局值。未来日期和超过各自阈值的指标都不会生成候选告警。

## 验证命令

默认命令只生成“未授权、未联网”的脱敏报告：

```cmd
cd /d D:\path\to\product-insight-copilot
.venv\Scripts\python.exe backend\scripts\verify_metric_source.py --tenant-id <隔离测试租户ID> --json-output docs\testing\artifacts\metric-source-local-check.json
```

确认地址、凭据、白名单和测试租户均属于隔离环境后，显式允许一次真实只读 GET：

```cmd
.venv\Scripts\python.exe backend\scripts\verify_metric_source.py --tenant-id <隔离测试租户ID> --allow-real-source-call --runs 3 --max-latency-ms 5000 --json-output docs\testing\artifacts\metric-source-result.json
```

报告只记录状态、行数、指标名和日期范围，不保存 URL、鉴权头、请求 ID 或指标值。即使探针
发布门禁要求生产 HTTPS、服务端鉴权、隔离租户、完整指标白名单覆盖，并至少连续成功读取 3 次；
证据只保留行数、版本存在性和脱敏延迟摘要，不保存 URL、鉴权头、请求 ID 或指标值。
成功仍只解除“真实指标源”这一项外部门禁，不能单独把整个项目的 `production_ready` 判为 true。

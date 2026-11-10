# 指标口径与报表说明（3.2.0 观测期）

| 项目 | 内容 |
| --- | --- |
| 归属知识库 | operation_kb |
| 文档版本 | 2.1.0 |
| 密级 | internal |
| 归属部门 | 数据运营部 |
| 数据源版本 | metric_snapshot_20260831 |
| 更新日期 | 2026-08-30 |

## 1. 指标定义

| 指标 | 定义 | 单位 | 可用维度 | 方向 |
| --- | --- | --- | --- | --- |
| next_day_retention 次日留存率 | 次日活跃去重用户数 / 当日新增用户数 | ratio | platform, app_version, channel, device | 越高越好 |
| crash_rate 崩溃率 | 当日崩溃次数 / 当日冷启动次数（含 java 与 native） | ratio | platform, app_version, channel, device | 越低越好 |
| dau 日活跃用户数 | 当日存在启动行为的去重用户数 | count | platform, app_version, channel | 越高越好 |

- 新增用户 = 当日首次 `device_register` 成功的去重设备数；次日活跃 = 该批用户在次日存在 `app_launch` 事件；
- **版本归属**：按用户当日上报的 app_version 归属，升级用户自升级当天起计入新版本，因此版本对比同时包含"版本效应"与"用户结构变化"；
- device 维度取值仅 `high_end` / `low_end`（RAM ≤ 4GB 或入门 SoC 记为 low_end，约占 Android 新增设备的 27%）；dau 不提供 device 维度，导出文件中该列留空。

## 2. 渠道口径

- `channel_a`：应用商店自然量 + 厂商预装，占 Android 新增约 69%，机型分布偏入门；
- `channel_b`：信息流与联盟广告投放，新用户价格敏感度更高。

## 3. 数据质量问题与处理约定（重要）

1. **2026-08-25 整日缺数**：上游明细任务调度失败，当日分区未产出，报表与导出文件整天缺失，未做回补。做趋势对比时应跳过该日或改用 7 日均值。
2. **2026-08-24 低端机小样本**：Android 3.2.0 + channel_a + low_end 当日样本仅 **312**（低于 500 阈值），该数值不具统计显著性，分析时应剔除该点。
3. **2026-08-26 重复行**：报表任务重跑，导致 Android 3.2.0 + channel_a + low_end 出现两条维度完全相同、取值略有差异的记录（0.223 与 0.217）。导入时应按 `(metric_name, platform, app_version, channel, device, stat_date)` 去重，保留任务重跑后的最新值。
4. **iOS 不产出 low_end 分群**：iOS 侧该分群为缺失而非 0，跨平台比较时必须对齐到 high_end 分群。

## 4. 报表刷新与使用注意

- 日报 T+1 09:00 刷新，数据来源为指标快照 `metric_snapshot_20260831`，导出文件与看板同源；
- 分群样本量 < 500 的单元格在报表中留空，原始明细仍保留数值；
- **二次口径差异**：本部门 crash_rate 含 native 崩溃，研发崩溃看板仅含 Java 层崩溃，两者数值不可直接相减或互相印证（详见 engineering_kb 的 3.2.0 故障复盘）；
- 报表导出文件可能存在重复行与缺日，导入前请执行第 3 节的去重与补数检查。

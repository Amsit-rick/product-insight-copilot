# 星野社区埋点字典 v1.4.0

| 项目 | 内容 |
| --- | --- |
| 归属知识库 | product_kb |
| 文档版本 | 1.4.0 |
| 密级 | internal |
| 维护部门 | 数据产品部 |
| 更新日期 | 2026-08-18 |
| 适用范围 | 3.1.9 / 3.2.0 双端 |

## 1. 公共字段

所有事件统一携带：`platform`(Android/iOS)、`app_version`、`channel`、`device_tier`、`user_id`（匿名设备 ID）、`report_time`、`session_id`。

`device_tier` 分级规则：`high_end` = RAM ≥ 6GB 或旗舰 SoC；`low_end` = RAM ≤ 4GB 或入门 SoC。低端机约占 Android 新增设备的 27%，iOS 不产出该字段分群。

## 2. 核心事件

| 事件名 | 触发时机 | 关键字段 | 主要用途 |
| --- | --- | --- | --- |
| `app_launch` | 应用进入前台（冷启动 / 热启动） | `launch_type`(cold/warm)、`first_launch` | crash_rate 分母、DAU |
| `device_register` | 首次启动完成设备注册 | `device_tier`、`channel` | 新增用户定义 |
| `user_login` | 登录流程到达终态 | `result`、`fail_code`、`duration_ms` | 登录成功率 |
| `app_crash` | 客户端崩溃 | `crash_type`(java/native)、`stage`(startup/foreground)、`stack_id` | crash_rate 分子 |
| `page_view` / `content_view` | 页面与内容浏览 | `page_id`、`content_id` | 首日内容消费 |

## 3. 指标口径映射

- **新增用户数**：`stat_date` 当天首次上报 `device_register` 成功的去重设备数。
- **次日活跃用户数**：上述新增用户中，在 `stat_date + 1` 当天至少产生一次 `app_launch` 事件的去重用户数。
- **next_day_retention = 次日活跃去重用户数 / 当日新增用户数**，可按 platform、app_version、channel、device_tier 拆分。
- **crash_rate = 当日 `app_crash` 事件数 / 当日冷启动次数**，**包含 java 与 native 两类崩溃**。
- **dau = 当日产生过 `app_launch` 事件的去重用户数**，不提供 device 维度。

## 4. 上报规则与延迟

- 实时事件（`app_launch`、`user_login`、`page_view`）上报延迟 ≤ 5 分钟；
- 崩溃事件分两段：java 崩溃实时上报；**native 崩溃由崩溃捕获 SDK 在下次启动时补报**，最迟 T+1 10:00 完成。因此 T 日的崩溃数据要到次日才完整；
- 若客户端在启动阶段失败（进程未进入前台），`device_register` 与 `user_login` 均不会上报，统计侧只能通过 `app_launch` 缺失反推；
- 单维度组合当日样本量 < 500 时不进入看板展示，仅保留在原始明细中。

## 5. 版本归属规则

活跃、留存与崩溃均按用户**当日上报的 `app_version`** 归属。用户升级后从升级当天起计入新版本，历史版本数据停止累积，因此跨版本对比会同时包含版本效应与用户结构变化。

## 6. 变更记录

- 1.4.0（2026-08-18）：新增 `device_tier`、`launch_type` 字段；明确 native 崩溃补报延迟与低样本展示规则。

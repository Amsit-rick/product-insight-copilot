# 星野社区 3.2.0 版本变更与接口变更记录

| 项目 | 内容 |
| --- | --- |
| 归属知识库 | engineering_kb |
| 文档版本 | 3.2.0 |
| 密级 | internal |
| 归属部门 | 研发中心 · 客户端组 |
| 发布信息 | 2026-08-20 09:00 Android 全渠道全量；iOS 同日提审，08-22 通过 |
| 更新日期 | 2026-08-22（发布后补充接口变更明细） |

## 1. 变更清单

| 编号 | 变更内容 | 影响 | 回滚方式 |
| --- | --- | --- | --- |
| ENG-1 | 登录 SDK 由 4.9.2 升级至 **5.4.0**（厂商主版本） | 支持运营商预取号与一键登录；初始化方式不兼容 4.x | 无法热回滚，需整包回退 3.1.9 |
| ENG-2 | **新增启动预热**：冷启动阶段并行预初始化登录 SDK 与推荐 SDK | 缩短首屏可交互时间 | 实验平台下发 `enableStartupPrewarm=false` |
| ENG-3 | **首屏加载顺序调整**：串行（登录 → 推荐 → 首屏）改为并行（先渲染骨架屏，再并行回填登录态与推荐流） | 首屏渲染提前 | 热修复开关 |
| ENG-4 | **移除低端机降级开关 `skipPrewarmOnLowEnd`**（3.1.9 对 RAM ≤ 4GB 机型默认跳过预热） | 低端机自 3.2.0 起同样执行完整预热流程 | 随 ENG-2 开关一并生效 |
| ENG-5 | 埋点 SDK 2.3.1 → 2.4.0 | 新增 device_tier / launch_type；**native 崩溃采集默认关闭，需服务端下发开关** | 保持服务端开关关闭 |

## 2. 开关与灰度状态

3.2.0 未做客户端灰度，08-20 09:00 全渠道全量。ENG-2、ENG-3 由实验平台服务端开关控制：08-20 全量开启；08-23 18:00 起对 30% 用户下发 `enableStartupPrewarm=false`，用于观察关闭预热后的留存表现。

## 3. 附录 A：登录 SDK v5.4.0 接口变更明细

- `LoginSDK.init(context, config)` 新增字段：`enableStartupPrewarm`(Boolean，默认 true)、`prewarmTimeoutMs`(Int，默认 3000)、`useNativePrewarm`(Boolean，默认 true)；
- 旧重载 `LoginSDK.init(context)` 标记废弃，内部转为 `useNativePrewarm=false` 的兼容实现；
- 新增回调：`onPrewarmFinished(prewarmCostMs)`、`onInitFailed(code, stage)`，其中 `stage` 取值为 `startup` 或 `foreground`；
- 一键登录获取 token 的超时时间由固定 5000ms 调整为按 `prewarmTimeoutMs` 派生（默认 3000ms）；
- 相关接口：`POST /v2/login/prewarm`（预取号）、`POST /v2/login/token`（换取登录态），两者共用同一预热会话 ID；
- 厂商 release note 记录：`useNativePrewarm=true` 时，登录 SDK 会创建 native 工作线程用于预取号，线程调度策略由系统决定。

## 4. 已知问题（未闭环）

- 内部测试阶段，在 Android 8.0 及以下、RAM ≤ 4GB 机型上出现过 3 例启动阶段 native abort（crashpad 日志），当时判定为测试机环境问题，未纳入发版阻塞项；
- 受 ENG-5 影响，native 崩溃采集默认关闭，该问题在 3.2.0 线上常规崩溃看板中不可见；
- 跟踪单 XC-2041（优先级 P2，状态：未闭环）。

## 5. 变更记录

- 2026-08-22 v3.2.0：补充附录 A 接口变更明细与已知问题说明；
- 2026-08-20 v3.2.0：首版随包发布。

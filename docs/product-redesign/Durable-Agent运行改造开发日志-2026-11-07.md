# Durable Agent 运行改造开发日志

日期：2026-11-07

## 本轮完成

### P0-1 统一工具副作用与重放契约

- 新增 `ReplayPolicy`：`safe / idempotent / unsafe`。
- 新增 `SideEffectLevel`：`read_only / reversible / irreversible`。
- 工具声明增加 `idempotency_scope` 和 `requires_approval`。
- 现有 `retrieval.search`、`metric.query`、`order.facts` 明确声明为 `safe + read_only`。
- 幂等工具缺少稳定键时失败关闭。
- unsafe 工具在 `replaying=True` 时始终禁止执行；人工确认后必须通过新的非重放动作执行。
- 工具调用审计新增重放策略、副作用级别、幂等键、是否重放和审批引用。

### P0-2 阶段级恢复记录

- `AnalysisTask` 新增：
  - `current_phase`
  - `checkpoint_version`
  - `phase_attempt`
  - `resume_count`
  - `resumed_from_checkpoint`
  - `wake_reason`
  - `state_revision`
- 建立业务阶段映射：理解问题、制定计划、事实查询、证据校验、形成建议、复核修订、人工决定、最终结果和完成。
- `load_task=False` 的证据合并、反思等节点也通过短事务更新阶段，不再只显示早期 Agent 节点。
- checkpointer 正常恢复和 checkpoint 丢失降级恢复分别记录来源与唤醒原因。

### P0-3 Snapshot + Delta 实时协议

- 新增 `GET /api/v1/tasks/{task_id}/snapshot`。
- 新增 `GET /api/v1/tasks/{task_id}/stream`。
- SSE 首帧固定为完整 `snapshot`；后续使用 `delta`，终态使用 `done`，人工等待使用 `paused`。
- 快照包含业务任务状态、`state_revision`、事件游标、稳定 `event_seq`、Agent 运行摘要和工具安全契约。
- 前端 API 类型和 endpoint 已补齐，下一阶段可直接接入 Agent 协作面板。

### P1-1 持久化父子任务树与所有权

- 新增 `TaskExecutionUnit`，独立记录 workflow、Agent、工具、审批和后台任务的父子关系，避免把 Agent 子步骤伪装成普通业务任务污染任务列表与队列。
- 根执行单元 ID 与 `AnalysisTask.id` 一致；Agent、工具和审批通过真实 owner ID 幂等绑定。
- 明确 attached/background、cascade/independent 和 continue/block_parent/fail_parent 策略。
- 任务取消只传播到 cascade 单元，独立后台单元继续运行；执行树不取代 `AnalysisTask` 的业务状态权威。
- 新增 `GET /api/v1/tasks/{task_id}/tree`，实时快照同步内嵌 `task_tree`。
- 历史播放包新增 `execution_units`，可还原 workflow → Agent → 工具/审批所有权关系。
- 前端补齐递归 `TaskExecutionUnit` 类型、快照字段与 `taskApi.tree()`。

### P1-2 `expected_revision` 乐观并发控制

- 新增 `TaskRevisionConflict` 和数据库原子 `claim_revision()`，使用任务 ID、租户、当前 revision 与允许状态共同做条件更新。
- 取消、重试、任务审批和审批记录入口均支持可选 `expected_revision`；版本冲突返回结构化 409，包含期望版本与实际版本。
- 未传 revision 的旧客户端保持顺序兼容，但并发旧请求仍通过 CAS 只允许一个成功。
- 审批恢复消息携带内部已占用 revision，`NodeRuntime.record_resume()` 再次原子消费，阻止延迟或重复恢复。
- 过期恢复消息记录警告事件后安全跳过，Worker 正常确认，不进入重复重试或死信。
- 前端工作台、任务进度、报告和审核页面均提交当前任务 revision。

### P1-3 Agent 协作过程面板

- 新增业务化协作面板，直接读取实时快照中的持久化任务树，不在前端伪造 Agent、工具或审批数据。
- 第一屏显示当前阶段、参与/运行/完成/等待/异常数量，以及每个 Agent 的职责、处理内容和工具结果。
- 模型、Agent code、owner ID、失败策略、重放策略和副作用级别默认折叠，原技术时间线、表格和事件流继续保留。
- 人工审批执行单元单独显示等待状态与原因。
- 任务页首次加载和轮询同步刷新快照；快照暂时不可用时不阻断原有任务操作。
- 修复 375px 下固定侧栏挤压主内容的问题：小屏导航改为横向滚动顶部导航，桌面布局保持不变。

### P2-1 默认关闭的只读主动异常触发器

- 新增确定性指标异常扫描服务，只读取标准化指标快照并创建候选运行告警。
- 默认 `PROACTIVE_ANOMALY_ENABLED=false`；关闭时扫描接口返回结构化 409，不做读写副作用。
- 自动扫描不调用模型、不启动 Agent、不创建诊断任务、不访问外部网络，也不执行退款、改价或消息发送。
- 同租户、指标、日期和异常方向使用稳定指纹去重；候选告警保留数据集版本、来源版本、质量标记、窗口、方法和阈值。
- 管理员明确确认后，才通过 `proactive-alert:{alert_id}` 幂等创建并派发现有多 Agent 诊断任务。
- 状态查看、扫描和确认均要求 `admin:manage`；扫描、候选发现和人工确认写入审计。
- 系统状态页新增管理员专属触发器面板，展示默认关闭和安全边界，并允许确认候选或查看已创建任务。
- 不在每个 API 副本内启动定时线程；后续生产调度必须由单实例调度器显式调用扫描接口。

## 数据库兼容

- 新增 PostgreSQL 迁移 `013_durable_task_runtime.sql`。
- 新增 PostgreSQL 迁移 `014_task_execution_units.sql`，并同步更新全新安装基线 `001_init.sql`。
- SQLite `init_db` 增加存量表幂等补列与根执行单元回填，避免本地旧库因 `create_all` 不修改已有表而崩溃。

## 验证

- P1-2 相关回归：63/63 通过。
- 后端全量：1181/1181 通过。
- P1-3 相关后端回归：12/12 通过。
- 前端端到端冒烟：88/88 通过。
- P2-1 原始专项：8/8 通过。
- 主动触发器、运行告警与任务流相关回归：23/23 通过。
- P2-1 完成后的后端全量：1189/1189 通过。
- P2-1 完成后的前端端到端冒烟：89/89 通过。
- axe WCAG A/AA：14/14 页面状态通过。
- 前端类型检查与生产构建：通过。
- Python 编译检查、限定文件 `git diff --check`：通过。
- P0 证据：`docs/testing/Durable-Agent运行改造P0-2026-11-07.tdd.md`。
- P1-1 证据：`docs/testing/Durable-Agent任务树P1-1-2026-11-07.tdd.md`。
- P1-2 证据：`docs/testing/Durable-Agent并发控制P1-2-2026-11-07.tdd.md`。
- P1-3 证据：`docs/testing/Durable-Agent协作面板P1-3-2026-11-07.tdd.md`。
- P2-1 证据：`docs/testing/Durable-Agent主动异常触发器P2-1-2026-11-07.tdd.md`。

## 量化进度

按已确定改造顺序重新计量：

| 改造项 | 权重 | 状态 |
|---|---:|---|
| P0-1 工具重放安全契约 | 20% | 完成 |
| P0-2 阶段级恢复记录 | 15% | 完成 |
| P0-3 Snapshot + Delta | 15% | 完成 |
| P1-1 父子任务树与所有权 | 15% | 完成 |
| P1-2 `expected_revision` 并发控制 | 10% | 完成 |
| P1-3 Agent 协作过程面板 | 15% | 完成 |
| P2-1 只读主动异常触发器 | 10% | 完成 |

**Durable Agent 专项完成度：100%。**

项目原有口径保持不变：本地工程改造 **99%**，生产交付准备度 **84%**，`production_ready=false`。本轮新增的是耐久运行工程能力，未补齐真实事实 sandbox、真实 Webhook、真实模型 holdout、跨主机环境或真人客服证据，因此不能提高生产准备度。

## P3-1 独立单实例主动调度器

- 新增数据库租约表与 `SqlSchedulerLease`，支持互斥获取、心跳续租、过期接管、owner 校验、运行结果记录和安全释放。
- 新增独立 `app.proactive_scheduler` 进程，默认关闭；启用后扫描所有活跃租户，单租户失败隔离并逐租户审计。
- 自动阶段仍只创建候选运行告警，不调用模型、不启动 Agent、不创建诊断任务。
- 新增 Compose `proactive` profile；默认 `docker compose up` 不启动调度器，显式启用后仍由数据库租约保证单实例扫描。
- 管理员系统状态页显示独立进程运行状态、最近结果、候选数量和最近完成时间，不暴露 owner ID 或内部错误正文。
- 隔离 SQLite 真实进程单轮扫描得到 17 个候选，`alerts_without_task=17`、`proactive_tasks=0`，证明自动阶段没有越权派发。
- 调度器/容器加固专项 **16/16**、相关回归 **57/57**、后端全量 **1200/1200**、前端冒烟 **90/90**、axe **14/14** 通过。
- 常规 preflight 为 **24 项：10 PASS / 14 WARN / 0 FAIL**；类型检查、生产构建、Python 编译、Compose 静态解析和限定差异检查通过。
- 详细证据：`docs/testing/Durable-Agent单实例主动调度器P3-1-2026-11-07.tdd.md`。

Durable Agent 原定 P0 → P1 → P2 专项完成度保持 **100%**，不因为路线图之外的 P3-1 再向上累加。新增独立口径：**生产调度工程闭环 100%**。项目本地工程改造仍为 **99%**，生产交付准备度仍为 **84%**，`production_ready=false`。

## P3-2 PostgreSQL 独立进程崩溃接管演练（2026-11-08）

- 新增默认不连接数据库的验收脚本；必须显式授权并配置隔离 PostgreSQL，SQLite 会被拒绝。
- 在专用 Compose PostgreSQL 上应用 15/15 个迁移，启动独立租约持有进程并强制终止。
- 实测第二个竞争者在租约有效期内被阻止，3.0 秒租约到期后成功接管并释放。
- 演练使用随机租约名，不触碰正式调度租约或业务表；结束后租约行数量为 0。
- 脱敏证据不保存连接串、租约名或 owner ID，预发布报告识别为 `verified_local`。
- 发布镜像已包含验收脚本，构建与脚本存在性检查通过。
- GitHub Actions 单 runner 生产形态任务已加入相同演练和证据上传，但尚未由仓库 push 实际触发。
- TDD、发布报告与容器交付契约专项 **35/35**、后端全量 **1205/1205** 通过；常规 preflight 为 **24 项：10 PASS / 14 WARN / 0 FAIL**。
- Python 编译、Workflow YAML 解析、发布镜像构建、证据脱敏和限定差异检查通过；隔离 Compose 容器残留为 0。
- 详细证据：`docs/testing/Durable-Agent-PostgreSQL调度接管P3-2-2026-11-08.tdd.md`。

生产调度工程闭环保持 **100%**，本轮把“SQLite/单元逻辑正确”提升为“真实 PostgreSQL 独立进程接管已验证”。项目本地工程改造仍为 **99%**，生产交付准备度仍为 **84%**，`production_ready=false`；本机进程接管不能替代跨主机或托管 PostgreSQL 故障切换。

## P3-3 指标新鲜度门禁（2026-11-08）

- 审计发现扫描器以数据库最大日期作为窗口终点，却不判断该日期是否已过期，旧数据可能被误报为当前异常。
- 新增最大数据延迟配置，默认 2 天；陈旧数据和未来日期数据均跳过，不执行异常算法、不创建候选告警。
- 定时调度汇总新增 `metrics_skipped` 和 `skipped_reasons`，手工与定时扫描审计记录跳过数量。
- 管理员页面显示数据最大延迟和最近跳过数量；无候选但存在跳过项时明确提示数据门禁原因。
- 隔离演示库真实调度结果：扫描 0 个、跳过 2 个、陈旧原因 2 个、候选告警 0、主动诊断任务 0。
- 核心专项 **20/20**、加入容器契约后的最终相关回归 **27/27**、后端全量 **1207/1207** 通过。
- 浏览器冒烟 **90/90**、axe **4/4** 页面状态通过；前端类型检查与生产构建通过。
- Python 编译、Compose `proactive` profile 静态解析和限定差异检查通过；常规 preflight 为 **24 项：10 PASS / 14 WARN / 0 FAIL**。
- axe 首轮发现并修复主管质检页事实标签对比度不足，没有掩盖非本轮页面的真实失败。
- 详细证据：`docs/testing/Durable-Agent指标新鲜度门禁P3-3-2026-11-08.tdd.md`。

生产调度工程闭环保持 **100%**。本轮减少陈旧数据误报风险，但没有获得真实指标源或跨主机证据，因此项目本地工程改造仍为 **99%**，生产交付准备度仍为 **84%**，`production_ready=false`。

## P3-4 标准化只读指标源接入（2026-11-08）

- 新增默认关闭的标准化 HTTP 指标源，只通过固定 GET 端点读取租户、白名单指标和日期窗口。
- 跨租户行、非白名单指标、无效版本、超大响应和超行数批次全部拒绝；未知字段不会进入数据库。
- 调度器在扫描前同步指标，复用既有导入质量校验和幂等覆盖；同步失败时该租户不会继续扫描旧缓存。
- 新增分指标新鲜度配置，支持崩溃率 T+0、留存 T+2 等不同延迟约定，非法配置由 preflight 阻断。
- 新增显式授权探针；默认实测为 `blocked_not_authorized`、`external_network_used=false`、`production_ready=false`。
- 管理员页面显示指标事实源状态、分指标策略数量和最近写入行数，不回显 URL 或鉴权头。
- 最终审计补齐 PostgreSQL 64 字符版本字段边界，相关回归 **106/106**、后端全量 **1224/1224**、浏览器冒烟 **90/90** 通过。
- 前端类型检查、生产构建、Python 编译、Compose 静态解析通过；preflight 为 **25 项：10 PASS / 15 WARN / 0 FAIL**。
- 详细证据：`docs/testing/Durable-Agent只读指标源接入P3-4-2026-11-08.tdd.md`。

本地只读指标源工程能力完成度 **100%**，但真实连通验证仍为 **0%**；当前没有真实端点、凭据或隔离测试租户，未执行任何外部网络调用。项目本地工程改造仍为 **99%**，生产交付准备度仍为 **84%**，`production_ready=false`。

## 下一步

下一阶段需要用户提供真实指标 API 的标准化测试端点、鉴权方式、隔离租户和指标 SLA，执行一次显式授权只读探针并按真实延迟校准阈值；随后在不同主机或目标托管 PostgreSQL 环境复演租约竞争、网络中断和故障接管，并继续完成真实 OMS/支付/物流/售后 sandbox、外部 Webhook、真实模型 holdout、跨主机环境和真人客服测试。

量化进度保持：本地工程改造 **99%**，生产交付准备度 **84%**。上述外部证据未提供前，`production_ready=false`，不能声称项目已达到生产交付条件。

## P3-13 主系统本地集成闭环与真实模型复验（2026-11-09）

- 新增可重复启动的隔离联调模式：主系统通过 HTTP 查询本地事实/指标 Sandbox，保留已有真实模型配置，业务库、对象目录、缓存和检查点全部写入 `.tmp/local-integration`，不污染日常数据库。
- 联调模式显式启用独立 SQL 队列 worker、SQLite checkpointer、工单反思和自动安全门禁；默认不开主动异常诊断，避免合成指标自动创建模型任务。
- 修复 API 重启误伤独立 worker 任务的问题：队列模式不再把 `pending/running` 统一判成中断失败，由队列租约负责接管；新增真实 API lifespan 回归。
- 新增两个可执行验收器：一个验证主系统完整售后闭环，一个用两个实际 Python 进程验证队列任务与人工中断检查点恢复。恢复演练使用 stub 模型和全新临时库，不产生模型费用。
- 反思输入修正：高风险建议在复核阶段标记为 `pending`，不再错误写成“不需要审批”；同时归一化模型返回的矛盾组合 `human_review + stop_reason=passed`。
- 实际检索能力仍为 stub embedding、无 reranker；只运行了 8 条合成政策基准，不能替代真实向量模型 holdout。没有可用的 embedding 服务地址和凭据时，不伪装成 BGE-M3 已接入。
- 最终真实模型联调 **19/19**：7 个业务 Agent 均使用模型输出，单样本 33.27 秒 / 5,850 tokens；反思发现 3 项问题并转人工，主管权限门禁通过，60 行合成指标写入隔离库。
- 同机跨进程恢复 7 项断言全部通过；后端全量 **1308 collected / 1307 passed / 1 skipped**，Python 编译与限定差异检查通过。
- 详细证据：`docs/testing/主系统本地集成闭环P3-13-2026-11-09.tdd.md`。

本阶段改用可复核的分项口径，不继续沿用旧的 99%/84% 主观总比例：本地集成五项优先级中，主系统接 Sandbox、反思实跑、同机跨进程恢复、真实模型售后闭环均已有可执行证据；真实 embedding/reranker 接入仍未完成。

## P3-14 真实 Embedding / Reranker 本地闭环（2026-11-09）

- 新增独立 FastEmbed CPU ONNX 模型服务，接入 `BAAI/bge-small-zh-v1.5`（512 维）和
  `BAAI/bge-reranker-v2-m3-int8`，不把约 0.66GB 模型依赖塞入主 API 默认镜像。
- 隔离启动器增加真实语义模式，模型、Sandbox、API、Worker 按顺序启动；512 维数据库与旧 1024 维 stub 索引物理隔离。
- Embedding 与 Reranker 增加严格失败策略、鉴权和运行计数；网络错误、维度错误、部分重排结果和非有限数值均失败关闭。
- 首轮实测暴露 BGE 负 logits 被错误套用绝对相关度阈值，导致 Recall@5 只有 56.25%；未降低门槛，修复为召回分数负责拒答、logits 只负责排序。
- 最终真实模型基准 16/16 命中，`Recall@5=1.00`、`MRR=1.00`、Reranker 16/16、Embedding 失败/回退 0/0；CPU 查询 p95 2443.64ms。
- 模型身份、维度、排序和质量门禁 7/7 通过；后端全量 1321 collected / 1320 passed / 1 skipped，默认 stub 基准保持 8/8 和 v1 证据契约。
- 详细证据：`docs/testing/真实语义检索闭环P3-14-2026-11-09.tdd.md`。

本地集成五项优先级现为 **5/5（100%）**。这是本地工程闭环，不等于生产交付：合成政策集不是独立业务
holdout，CPU p95 约 2.44 秒未形成正式 SLO，且没有生产规模索引、目标硬件并发或外部向量库证据。因此
`production_ready=false`，不恢复旧的主观 99%/84% 总比例。

## P3-15 真实文档独立 Holdout 准备（2026-11-09）

- 新增真实 DOCX 语料准备器：只记录相对来源名、SHA-256 和解析统计，不复制源文件进仓库；生成物写入已忽略的 `.tmp`。
- 新增独立 Holdout 验证与真实模型评测器：人工独立性、脱敏、双人复核、语料指纹、源文件漂移、证据定位和业务阈值任一不满足即失败关闭。
- 评测命中升级为“来源文档 + 章节/证据片段”，不再把同文档错误章节计为正确。
- 修复 DOCX 失效关系兼容性：`python-docx` 失败时只读回退正文 XML；两条链路都失败时仍显式报错。
- `D:\Hadoop_data` 实测：11 个 DOCX、10 份唯一文档、1 份精确重复、0 份解析失败、5,085 个内容块、668 个切片、200 个草稿候选，候选覆盖 10/10 唯一文档。
- 未审核机器草稿实测被退出码 2 阻断，未调用模型，未生成 Recall/MRR。
- 文档 Holdout 与 Office 解析专项 20/20、后端全量 1,327 collected / 1,326 passed / 1 skipped，Python 编译和差异检查通过。
- Hadoop 教学资料不是售后业务语料；独立人工文档 Holdout 仍为 0 条已审核，售后真实 Holdout 也未完成。
- 详细证据：`docs/testing/真实文档Holdout准备P3-15-2026-11-09.tdd.md`。

P3-15 工具链与真实语料准备均为 **100%**，但独立人工 Holdout 尚未开始，`production_ready=false`。下一优先级为生产规模索引压测，结果必须区分合成容量证据与真实语义质量证据。

## P3-16 检索容量趋势与证据固化（2026-11-09）

- 加固 `benchmark_retrieval.py`：强制隔离 stub Embedding、内存缓存和 fallback 后端，新增输入/维度校验、错误门禁、可选 P95 门槛及默认 JSON 证据。
- 证据固定区分执行成功、业务验收和生产就绪；无业务阈值时写入 `acceptance_evaluated=false`，合成数据固定 `quality_claim_allowed=false`。
- 512 维合成容量实跑 1,000 / 4,000 / 10,000 切片，每档并发 1/4、各 12 次，共 72 次查询，错误 0。
- 10,000 切片单并发 P95 2,057.17ms、0.50 QPS；并发 4 P95 8,337.71ms、0.48 QPS。并发没有提高吞吐，证实 fallback O(n) 路径不适合作生产后端。
- 10,000 条预计算向量占 129,772,592 bytes；载入约 2,026 条/秒，但不含解析、切片或 Embedding 生成，不能称为完整索引吞吐。
- README 与向量存储注释已更新为本轮 512 维结果，移除旧 1024 维容量数字和 pgvector“未验证”过期描述。
- 压测器与向量存储专项 14/14、后端全量 1,331 collected / 1,330 passed / 1 skipped；Python 编译、JSON 解析和差异检查通过。
- 结构化证据：`docs/testing/artifacts/retrieval-capacity-2026-11-09.json`；详细说明：`docs/testing/检索容量趋势P3-16-2026-11-09.tdd.md`。

P3-16 本地工具链与 SQLite 容量趋势完成度 **100%**；目标生产向量库规模验证仍为 **0%**。下一优先级为真实 Embedding/Reranker 并发与吞吐压测，`production_ready=false`。

## P3-17 本地真实模型并发与负优化回退（2026-11-09）

- 新增真实本地 ONNX 模型服务压测器，覆盖 Embedding 单条/批量、Reranker 8 候选和两者交错的混合负载；输出脱敏 JSON 并失败关闭。
- 健康状态新增 `model_threads=4` 和 `inference_policy=shared_serial`，性能证据可对应实际运行策略。
- Embedding 单条并发 4 P95 14.14ms、264.68 QPS；批量 8 并发 2 P95 55.30ms、42.60 请求/秒（约 341 文本/秒）。
- Reranker 并发 1/2/4 的 P95 分别为 360.37/721.59/1,426.13ms，吞吐始终约 3 QPS，是本地完整检索的主要模型瓶颈。
- 尝试拆分 Embedding/Reranker 推理锁后，混合并发 4 的 Embedding P95 从 725.33ms 降到 11.55ms，但 Reranker P95 从 724.51ms 恶化到 1,426.52ms，总 QPS 从 6.17 降到 5.79；已回退该负优化。
- 项目已按共享串行策略重新启动，8033 API、8091 Sandbox、8093 模型服务均健康。
- 模型服务、压测器和联调专项 17/17、后端全量 1,336 collected / 1,335 passed / 1 skipped；Python 编译、JSON 解析和差异检查通过。
- 结构化证据：`docs/testing/artifacts/local-model-concurrency-2026-11-09.json`；详细说明：`docs/testing/本地真实模型并发P3-17-2026-11-09.tdd.md`。

P3-17 本地压测工具链与 CPU ONNX 并发特征完成度 **100%**；目标硬件、多实例或动态批处理验证仍为 **0%**。下一优先级为目标部署环境验证，`production_ready=false`。

## P3-18 目标部署单机验证与容器加固（2026-11-09）

- 新增生产运行时硬门禁：生产环境拒绝 DEBUG、SQLite、非 Redis、危险 CORS、示例数据库口令和默认对象存储凭据；原有 JWT、自动播种、共享存储、队列和 Postgres checkpointer 门禁继续保留。
- Node、Python 和单机基础设施镜像默认按 digest 固定；移除会强制访问远程 Dockerfile frontend 的无必要语法声明，修复离线缓存命中时仍被 Docker Hub 超时阻断的问题。
- 应用容器启用非 root、只读根文件系统、`cap_drop=ALL`、`no-new-privileges`、独立 tmpfs 和 CPU/内存/PID 上限；PostgreSQL、Redis、MinIO、Qdrant 同步增加资源边界。
- Worker 新增无业务数据的文件心跳，两个真实 Worker 容器均为 healthy，不再只凭进程存在判断存活。
- 新增脱敏 Compose 探针，绑定运行镜像与 Git SHA，并明确拆分运行形态、应用加固、整体部署加固、跨主机和生产就绪状态。
- 全新 `pic-target-verify` 隔离环境实测：15/15 迁移、pgvector 0.8.6、Redis、MinIO、API 和两个 Worker 全部通过；旧 `pic-ticket-audit` 未停止、重建或清卷。
- 8 个真实 HTTP 工单任务全部完成、错误 0，两个 Worker 均领取任务，端到端 P95 4384.53ms，最终队列无 pending/claimed/dead。
- 严格 preflight 为 25 项：13 PASS / 1 WARN / 11 FAIL；失败项均为真实模型/真实平台或正式生产配置，未被本地容器通过掩盖。
- 后端全量 1352 collected / 1351 passed / 1 skipped，专项、Python 编译、Compose 解析、真实镜像构建和差异检查通过。
- 结构化证据：`docs/testing/artifacts/compose-deployment-2026-11-09.json`、`docs/testing/artifacts/compose-deployment-queue-summary-2026-11-09.json`；详细说明：`docs/testing/目标部署单机验证P3-18-2026-11-09.tdd.md`。

P3-18 单机部署运行形态与应用容器加固完成度 **100%**。整体部署加固仍未就绪：应用镜像尚未使用受控制品仓库 digest，Python 依赖也未形成哈希锁；跨主机、真实模型、真实事实平台和人工批准仍为 **0%**。`production_ready=false`，下一优先级为不可变应用制品与依赖锁定。

## P3-19 Python 依赖锁与不可变应用制品（2026-11-10）

- 新增 Linux 生产运行时锁、Linux/macOS/CI 开发锁和 Windows 开发锁；三套锁均为精确版本并包含 SHA-256，安装统一使用 `--require-hashes`。
- 生产运行时锁 92 包且不含 pytest；两套开发锁各 97 包。Windows 全新 Python 3.12 虚拟环境真实安装通过，明确排除不支持 Windows 的 `uvloop` 并保留 `colorama`。
- Dockerfile 只安装运行时锁，OCI revision 绑定完整 Git SHA；revision 标签移动到镜像末端，避免提交号变化使依赖缓存失效。
- 新增 release Compose 覆盖，五个应用服务全部移除源码 `build`，要求 `name@sha256:digest` 镜像；CI 和本地脚本同步加入锁与发布编排门禁。
- 新增镜像归档与 manifest，最终镜像 digest 为 `pic-api@sha256:968aff6743cbc770e17db3251589817fd736f7fc7a82ab5a17978ecc22bdd123`，镜像内无 pytest，以 `appuser` 运行。
- 首轮空卷运行暴露 MinIO health 与 S3 可连接之间的竞态；初始化增加有界重试后，全新空卷复验迁移和 MinIO 初始化均退出 0。
- 隔离 `pic-immutable-verify` 中 PostgreSQL、Redis、MinIO、API 和两个 Worker 全健康；部署探针确认 `application_image_immutable=true`、`deployment_hardening_ready=true`。
- 8 个真实 HTTP 工单任务完成 8、错误 0，两个 Worker 均消费；端到端 P95 4302.28ms，最终队列清空。
- 依赖与交付专项 28/28、后端全量 1361 collected / 1360 passed / 1 skipped、脚本语法、Compose 解析、Python 编译和差异检查通过。
- 详细证据：`docs/testing/不可变制品与依赖锁P3-19-2026-11-10.tdd.md`。

P3-19 本地依赖锁与不可变制品闭环完成度 **100%**。企业制品仓库推送/拉取验证与跨主机目标环境验证仍为 **0%**；本地 digest 和归档不能冒充企业仓库证据。真实事实平台、正式模型 holdout 和人工发布批准仍未完成，`production_ready=false`。下一优先级为受控制品仓库发布、目标节点按 digest 拉取与回滚演练。

## P3-20 本地 Registry 发布与应用镜像回滚（2026-11-10）

- 新增回环地址专用 OCI Registry，镜像按 digest 固定并配置 CPU、内存和 PID 上限；明文访问只允许显式授权的 localhost/回环 IP。
- 新增制品验证器：校验源 OCI revision，执行 push，从 Registry API 获取远端 digest，删除本地 Registry 引用后按 digest 拉取，并复验 revision、RepoDigest 和非 root 用户。
- 新增完整演练脚本：当前/上一镜像发布与拉取、当前 digest 上线、部署探针、双 Worker 四单队列探针、上一 digest 回滚及二次部署/队列探针。
- 首轮演练暴露 API 已 ready 而 Worker 仍处于 `health: starting` 的启动时序；增加双 Worker health 有界等待后完整流程通过。
- 当前版本 4/4 完成、错误 0、两个 Worker 消费，提交 P95 1237.66ms、端到端 P95 4195.69ms；回滚版本 4/4 完成、错误 0，提交 P95 1195.60ms、端到端 P95 3533.69ms。
- 当前与回滚版本的 23 项部署检查均通过，迁移、共享 checkpointer、共享对象存储、非 root、只读根文件系统、能力移除、禁止提权、资源限制和 digest/revision 绑定均通过。
- 演练自动清理临时容器、网络和卷；脱敏证据不包含 Worker ID、run ID、密码或令牌。
- Registry 与容器交付专项 22/22、后端全量 1370 collected / 1369 passed / 1 skipped，Python 编译、PowerShell 语法、Compose 解析和差异检查通过。
- 详细证据：`docs/testing/本地Registry发布回滚P3-20-2026-11-10.tdd.md`。

P3-20 本地 Registry 发布、按 digest 拉取、当前版本上线和应用镜像回滚演练完成度 **100%**。企业制品仓库、跨主机目标节点和数据库迁移回滚验证仍为 **0%**；本轮使用明文无认证回环 Registry、同一 Docker daemon、stub 模型和 mock 事实适配器，不能冒充生产证据。当前 `production_ready=false`。下一优先级为带 TLS/认证/审计的企业 Registry、镜像签名或 provenance、独立目标节点拉取和跨主机滚动升级/回滚。

## P3-21 镜像供应链证明与 SBOM（2026-11-10）

- Buildx 构建启用 `provenance=mode=max` 和 SPDX SBOM，新增失败关闭的镜像证明验证器与本地回环 Registry 演练脚本。
- 验证 OCI index、应用/attestation manifest 关联、OCI revision、非 root、构建参数与 VCS SHA/source、运行时间、resolved dependencies 和 SPDX 2.3 包清单。
- Docker driver 的空 builder ID 被识别为真实兼容差异：builder 对象仍必须存在，非空 ID 只输出指纹，空值记录 `builder_identity_available=false`。
- `runDetails.metadata.vcs` 在同一 driver 中可能省略；根请求 VCS 始终作为硬门禁，冗余 metadata VCS 存在时必须一致，缺失时明确记录，不降低 OCI revision、构建参数或根 VCS 的 SHA 绑定。
- GitHub Actions 新增供应链任务并把第四类附件纳入 release evidence bundle；但仓库当前唯一远端为 Gitee，尚无真实 GitHub Actions 运行，不能把 workflow 文件冒充远程 CI 证据。
- 最终本地镜像绑定提交 `0ed104dd4d1b607443c5f9587d0d36e8dec9e313`，SPDX 包 198、resolved dependencies 3，provenance/SBOM 校验通过。
- 专项 **51/51**、后端全量 **1386 collected / 1385 passed / 1 skipped**；Python 编译、PowerShell 语法、Workflow YAML、Compose 解析、证据敏感字段扫描和差异检查通过。
- 结构化证据：`docs/testing/artifacts/image-attestations-p3-21-2026-11-10.json`；详细说明：`docs/testing/镜像供应链证明P3-21-2026-11-10.tdd.md`。

P3-21 本地工具、契约和真实构建/验证闭环完成度 **100%**。按十项任务拆分，总体为 **90%（9/10）**，剩余真实远程 CI 运行证据；镜像签名、企业 Registry、跨主机验证均为 **0%**，`production_ready=false`。下一阶段必须先选择实际 CI 平台：GitHub 镜像仓库运行现有 workflow，或补齐 Gitee 等价流水线，再继续签名和独立节点验证。

## P3-22 跨平台供应链 Runner（2026-11-10）

- 新增平台无关 Bash 脚本，统一执行 Git SHA 绑定、回环 Registry、Buildx provenance/SBOM 构建、push、验证和退出清理。
- GitHub Actions 改为调用统一脚本，避免 workflow 与本地演练出现两套实现。
- 新增 Gitee Go 导入模板，要求显式配置 Docker 自托管 host group；占位 ID 未替换时失败，不创建虚假的活动流水线。
- WSL 首轮实跑暴露 Docker Desktop 集成缺失，且失败说明文字被误判为项目占用；修复为独立校验 Docker daemon、Buildx、Compose 及 Compose 查询退出码。
- 使用 `docker:27-cli` Linux 容器、Docker socket 和 host 网络真实执行完整脚本，镜像绑定提交 `ff8f68eff077aed6ed6846c755fdc4ca49824986`，SPDX 包 198、resolved dependencies 3，provenance/SBOM 通过。
- Linux Runner 演练仍共享本机 Docker daemon，证据 scope 固定为 `local_loopback_registry`，未冒充 Gitee 托管运行或跨主机证据。
- CI/交付专项 **36/36**、修复后容器交付专项 **19/19**、后端全量 **1388 collected / 1387 passed / 1 skipped**；Bash、YAML、JSON、敏感字段和差异检查通过。
- 结构化证据：`docs/testing/artifacts/linux-runner-image-attestations-p3-22-2026-11-10.json`；详细说明：`docs/testing/跨平台供应链Runner-P3-22-2026-11-10.tdd.md`。

P3-22 本地跨平台 Runner 工程闭环完成度 **100%**。Gitee Go 真实 host group、push 后远程运行和 artifact 复验仍为 **0%**，所以 P3-21 总体仍为 **90%（9/10）**，`production_ready=false`。下一步需要真实仓库平台权限和 Runner 主机组，不能由本机 Docker 演练替代。

## P3-23 镜像签名与验签闭环（2026-11-10）

- 新增 Cosign JSON claims 验证器，强制校验 `name@sha256:digest`、manifest digest、完整 Git SHA 和 `release_sha` 注解。
- 签名证据只记录 16 位 SHA-256 公钥指纹，固定区分透明日志、keyless、KMS、企业 Registry、跨主机和生产就绪状态。
- 统一 Bash 供应链脚本增加 `SIGN_IMAGE=true` 分支；Cosign v3.1.4 镜像按 digest 固定，私钥写入一次性 Docker volume，不落工作区。
- 使用随机密码和显式离线 signing config；退出时清理签名卷、临时目录、Registry 和 Compose 资源。
- GitHub Actions 与 Gitee Go 模板均增加独立签名证据；release evidence bundle 强制附件从 4 类增加到 5 类。
- 首轮验证发现 GHCR digest 为 riscv64 单架构、Cosign v3 移除旧参数、非 root Cosign 无法写新卷三个真实问题，分别通过多架构 index、离线 signing config 和固定 digest Alpine 初始化卷权限解决。
- Linux Runner 形态真实演练通过：镜像 digest `sha256:a87d80cce2b920943d786a47e27dd1f0058c0851c311bfafc5e6338d16fef143`，签名数量 1，公钥指纹 `eb4f8521e4f86ffa`，签名和 `release_sha` 注解均验证通过。
- provenance/SBOM 同轮通过：应用 manifest `sha256:638cb6395d21a033b057fdd47bfd13ca803118bd22c0d43551538e012418cb95`，attestation manifest `sha256:5acd3f9498da3043383c05cd0146df5fdcedd8e1e89e389636c5e46bc9af3531`，SPDX 包 198、resolved dependencies 3。
- P3-23 专项 **37/37**、后端全量 **1394 collected / 1393 passed / 1 skipped**；Python、Bash、YAML、JSON、敏感字段和差异检查均通过。
- 结构化证据：`docs/testing/artifacts/image-signature-p3-23-2026-11-10.json`、`docs/testing/artifacts/image-attestations-p3-23-2026-11-10.json`；详细说明：`docs/testing/镜像签名验签P3-23-2026-11-10.tdd.md`。

P3-23 本地镜像签名验签闭环完成度 **100%**。企业 KMS、Sigstore keyless 身份、透明日志、企业 Registry 和跨主机验证仍为 **0%**；本轮使用一次性本地密钥、回环 Registry 和共享 Docker daemon，不能冒充企业发布证据，`production_ready=false`。下一优先级为真实 Gitee Runner 运行和远程 artifact 复验，再进入企业密钥与目标节点准入治理。

## P3-24 Gitee 远程流水线准备（2026-11-10）

- 将 P3-23 提交和活动流水线推送至 Gitee，通过官方接口注册 `.workflow/pic-supply-chain.yml`，仅允许手工触发。
- 新增 JUnit 脱敏证据生成器，固定区分远程契约通过、镜像供应链未执行、企业 Registry/跨主机未验证和生产未就绪。
- 新增 POSIX shell Docker 能力探针，分别检查 CLI、daemon、Buildx 与 Compose；先输出 artifact，再由独立步骤失败关闭。
- 能力探针支持 `gitee_go/remote_ci_cloud_runner` 与 `local_linux_container/local_shared_docker_daemon` 两种显式范围，阻止本地演练冒充远程证据。
- 前两次远程执行因 Gitee 节点无法拉取 Docker Hub Python 镜像失败；改用官方 Python 执行器后，完整开发依赖下载过重，已主动取消并收敛为最小哈希锁。
- 新增 `requirements-ci-contract.lock` 和 `--confcutdir=backend/tests/unit`，隔离完整应用夹具；全新 Windows 临时环境完成哈希安装、`pip check` 和契约测试。
- 最终远程提交 `aec4c1b6a9d5eb67642960bf44ce11a086e2a1b5`：契约 **45/45**，Docker 能力探针成功上传 artifact，提交 SHA 和两份远程产物均已下载复验。
- Gitee 托管执行器不提供 Docker CLI、daemon、Buildx 或 Compose；独立门禁在 `command -v docker` 处失败关闭，整条流水线最终失败是预期安全结果。
- 全量回归首次有两个子进程测试因本机低内存触发 OpenBLAS 分配失败；测试夹具固定数值库单线程后专项 **4/4**、后端全量 **1403 collected / 1402 passed / 1 skipped / 0 failed**。
- 结构化证据：`docs/testing/artifacts/gitee-go-remote-readiness-p3-24-2026-11-10.json`；详细说明：`docs/testing/Gitee远程流水线准备P3-24-2026-11-10.tdd.md`。

P3-24“远程环境能力判定与 artifact 复验”完成度 **100%（5/5）**。Gitee 托管执行器的远程镜像供应链执行能力仍为 **0%**，不能把 Python 契约成功冒充镜像构建、provenance/SBOM 或签名验签成功。下一阶段需要具备 Docker 能力的自托管 Runner/企业主机组或等价 CI 平台；企业 KMS、企业 Registry、跨主机与生产验证仍为 **0%**，`production_ready=false`。

## P3-25 自托管 Runner 准入与证据绑定（2026-11-10）

- 新增自托管 Runner 准入验证器，覆盖 Git/Bash/curl/Python、Docker daemon、Buildx、Compose、提交一致性、干净工作区、Linux/amd64、非 root、本地 Docker endpoint、内存和磁盘门槛。
- 主机组 ID 不进入日志或证据，只保存 16 位 SHA-256 指纹；占位 ID、root 自托管 Runner、远程 TCP daemon 和资源不足均失败关闭。
- 新增统一入口，依次执行 Runner 准入、provenance/SPDX 构建、Cosign 签名验签和证据打包；Runner、attestation、signature 必须绑定同一提交和同一镜像 digest。
- Gitee 企业主机组模板升级为上传四份 artifact；模板仍非活动流水线，未配置虚假 host group。
- 脏工作区真实负向验证退出码 2；P3-25/容器交付专项 **31/31**。
- 使用按 digest 固定的 `docker:27-cli` Linux 容器、只读源码、独立 `.tmp`、共享 Docker socket 和回环 Registry 完整实跑。
- 提交 `10a782113b8b5289ca6329ae2daa1e3b9de08f95` 的镜像 index digest 为 `sha256:f311de65b37bb0600ef6679e368fe2b31276ee78e20281acd8284c0321a8bcd3`；provenance、SPDX 198 包、签名和 `release_sha` 验证通过。
- bundle 明确记录 `self_hosted_runner_verified=false`、`local_runner_rehearsal_verified=true`；演练后容器、网络和卷均为 0。
- 后端全量 **1413 collected / 1412 passed / 1 skipped / 0 failed**；Python、Bash、YAML、JSON、证据哈希、敏感字段和差异检查通过。
- 结构化证据：`docs/testing/artifacts/local-runner-supply-chain-bundle-p3-25-2026-11-10.json`；详细说明：`docs/testing/自托管Runner准入与证据绑定P3-25-2026-11-10.tdd.md`。

P3-25 总体完成度 **80%（4/5）**，本地可开发部分 **100%**。企业 Gitee Go 主机组远程运行与 artifact 复验仍为 **0%**；企业 Registry、KMS/keyless、透明日志和跨主机验证仍为 **0%**，`production_ready=false`。

## P3-26 Runner 加固与 P3-27 安全 Registry（2026-11-10）

- Runner 新增 `timeout` 工具准入、仓库 `.tmp` 原子目录锁、完整 SHA 校验和共享截止时间；准入、镜像供应链和证据打包使用同一剩余时间预算。
- 新增陈旧锁恢复器：默认 dry-run，只接受 `pic-*` 项目，拒绝新锁和存活 PID；apply 只清理指定 Compose 项目，不使用全局 prune。
- 并发锁真实负向验证内部退出码 3；陈旧锁 dry-run 和 apply 均通过，指定项目与锁清理完成。
- Linux Runner 完整回归绑定提交 `089504ea2876cb8a956fa5998879d72b00ba1901`，镜像 digest `sha256:fcd79c7da7b1d4890b01fc11ec83b163ace188c0c7f596e61008baad2e16e754`；provenance、SPDX 198 包、签名和 bundle 全部通过。
- 新增一次性 CA、服务证书和 bcrypt htpasswd 生成器；TLS/Auth Registry 只绑定回环地址，使用只读根文件系统、能力移除、禁止提权和资源限制。
- Registry 验证器不修改系统信任库，通过 HTTP API 真实上传 blob/manifest，并按 digest 拉取复验；未信任 CA、无凭据和错误凭据均失败关闭。
- 首轮安全 Registry 演练暴露 Windows PowerShell 5.1 CSPRNG 静态重载不兼容，改为实例式随机数生成器后完整通过。
- P3-27 绑定提交 `509d909ce21a52fcfc65cc102655e175dd387f28`，manifest digest `sha256:2f77ded70af1c0625fbeae1f88bbe407b43d73b82ec2d1f7ad99ecde666f174c`；临时证书、私钥、密码、容器、网络和卷均已清理。
- 专项 **39/39**、后端全量 **1427 collected / 1426 passed / 1 skipped / 0 failed**；Python、PowerShell、Bash、Compose、敏感信息和差异检查通过。
- 详细说明：`docs/testing/Runner加固与安全Registry-P3-26-P3-27-2026-11-10.tdd.md`。

P3-26 + P3-27 本地改造完成度 **100%**。企业自托管 Runner、企业 Registry、KMS/keyless、透明日志和跨主机目标节点仍为 **0%**，不能由本机回环演练替代，`production_ready=false`。

## P3-28 目标节点发布准入门禁（2026-11-10）

- 新增目标发布准入验证器，统一读取 Runner、provenance/SBOM、签名、供应链 bundle、TLS/Auth Registry、发布回滚和目标环境七类证据。
- 验证 schema、文件大小、bundle 内部哈希、完整 Git SHA、镜像 digest、时效、企业/本地作用域和所有输入的 `production_ready=false`。
- 输出七份输入 SHA-256 和 `admission_bundle_id`，固定本次判定批次；强制模式在机器准入未通过时返回退出码 1。
- 所有机器证据即使通过也只能进入 `machine_ready_awaiting_human_approval`，工具永远不设置 `release_authorized=true`。
- 单元测试覆盖本地证据不越权、混合提交、证据篡改、自称生产就绪以及完整企业机器证据仍需人工批准。
- P3-28 单元测试 **4/4**、P3-28 与容器/远程契约专项 **26/26**、后端全量 **1431 collected / 1430 passed / 1 skipped / 0 failed**；Python、Workflow YAML、JSON、敏感信息和差异检查通过。
- 当前候选提交 `c7f454df2eb10275f1172be4647f789d143aff2e` 的真实判定为 `blocked`：现有证据来自 `089504e`、`509d909`、`f3a6586` 三个批次，rollout 与供应链 digest 不一致，旧目标环境证据不满足时效，企业 Registry 和跨主机仍未验证。
- 结构化证据：`docs/testing/artifacts/target-release-admission-p3-28-2026-11-10.json`；详细说明：`docs/testing/目标节点发布准入门禁P3-28-2026-11-10.tdd.md`。

P3-28 工具与本地验证完成度 **100%**；当前候选版本机器准入完成度 **0%**。门禁明确阻止跨提交、跨 digest 拼接证据，`production_ready=false`。

## P3-29 单一候选版本证据再生（2026-11-10）

- 最终代码候选固定为 `852d4d7629a7bd43cdc0d95fa564e44acb15177b`，目标探针、rollout、Registry 控制和统一准入均绑定该完整 SHA。
- 修复 Windows PowerShell 5.1 对 Docker inspect format 嵌套引号的破坏，改为反序列化完整 JSON；rollout 新增 API Docker health 等待，消除 HTTP 已就绪但容器 health 仍为 `starting` 的竞态。
- 使用与 Runner 相同的 Docker 27 / Buildx 前端重新生成 rollout 镜像，应用 manifest 精确匹配 `sha256:e8c82baf4bd0ed1794cfa9d364d57b66c2457e5a5d063701d1ceed6ec97de138`。
- 同轮供应链证据通过：index digest `sha256:bde9b115ecb6bc095b7caa5702a6e70d7dc16a9a228cba87cf308168104e2b75`，SPDX 2.3 共 198 包，Cosign 签名和 `release_sha` 注解通过。
- 最终 SHA 的本地 TLS/Auth Registry 通过未信任 CA、无凭据和错误凭据拒绝，以及认证推送和 digest 拉取复验；企业 Registry 仍未验证。
- 当前候选部署、双 Worker 4/4 队列、历史候选回滚、回滚后双 Worker 4/4 队列以及恢复当前候选全部通过，错误和死信均为 0。
- 实时目标探针确认 `test` 环境、候选 SHA、数据库、Redis、队列、共享 checkpointer 和共享存储全部通过。
- 统一准入的 SHA、digest、bundle 哈希、时效、供应链、Registry 控制、本地 rollout 和目标运行形态全部通过；唯一阻断项为 `enterprise_registry_verified` 和 `cross_host_verified`，强制模式正确返回退出码 1。
- 专项 **81/81**、后端全量 **1433 collected / 1432 passed / 1 skipped / 0 failed**；Python、PowerShell、Bash、Workflow YAML、JSON、artifact 复验和差异检查通过，P3-29 容器、网络和卷残留为 0。
- 结构化证据：`docs/testing/artifacts/*p3-29-2026-11-10.json`；详细说明：`docs/testing/单一候选版本证据再生P3-29-2026-11-10.tdd.md`。

P3-29 本地工程与证据再生完成度 **100%**；统一机器准入检查 **10/12（83.3%）**。企业 Registry、跨主机、企业 KMS/keyless 和透明日志仍为 **0%**，`release_authorized=false`、`production_ready=false`。

## P3-30 数据库发布安全与 P3-31 统一准入消费（2026-11-10）

- rollout 新增 current / previous 镜像迁移清单比对、数量与 SHA-256 证据；迁移集合不同则失败关闭，不允许只验证应用镜像回滚后继续发布。
- 当前候选与上一候选均有 15 个迁移文件且清单一致，真实结论为“本次不需要数据库迁移回滚”；保持 `database_migration_rollback_exercised=false`，没有把未执行的恢复演练写成完成。
- 统一目标准入将 `database_release_safety_verified=true` 纳入本地 rollout 硬门禁。
- 最终发布报告升级为 `after-sales-release-readiness-v5`，发布证据升级为 `after-sales-release-evidence-v2`，交付检查升级为 `after-sales-delivery-check-v5`；最终消费者必须读取 canonical 统一准入证据。
- 真实 rollout 首次暴露 Linux `find -exec` 分号转义缺陷，流程在部署前失败；补充契约测试并修复为 `\;`，提交 `d6bff1e08451cbb9384450ad8db6f637864d4598`。
- 最终候选供应链 index digest 为 `sha256:b97ed4310a9746ad9781aedee10dd7caed78a83f9aaadcd5775849e52c827d60`，linux/amd64 应用 manifest 为 `sha256:e3604c318bb2a0a2a12fc1696e152cdcbcf904d7c72b7b5a316ad69082dd89cd`，SPDX 198 包和 Cosign 签名通过。
- 多架构 index 直接用于 rollout 时被 `image_digest_verified` 正确阻断；改为用同一 Docker 27 / Buildx 前端导出无附加 attestation 的单平台运行镜像，应用 manifest 精确匹配，没有降级门禁。
- 当前候选和回滚候选均完成双 Worker 4/4 队列验证，错误与死信为 0；恢复当前候选后的目标节点实时探针通过。
- 统一准入 12 项中 10 项通过，唯一阻断为企业 Registry 与独立跨主机；强制模式退出码 1，`release_authorized=false`、`production_ready=false`。
- v5 最终报告确认准入证据新鲜且提交匹配，但机器准入未完成；v5 交付检查增加 `target_release_admission` 阻断项，交付 manifest 离线复验通过且不信任生产就绪声明。
- 专项 **138/138**、后端全量 **1436 collected / 1435 passed / 1 skipped / 0 failed**；Python、PowerShell 8/8、Bash 4/4、YAML 3/3、JSON 17/17、证据复验和差异检查通过，P3-31 容器、网络和卷残留为 0。
- 详细说明：`docs/testing/数据库发布安全与统一准入P3-30-P3-31-2026-11-10.tdd.md`；结构化证据：`docs/testing/artifacts/*p3-31-2026-11-10.json`。

P3-30 / P3-31 本地工程与证据闭环完成度 **100%**；统一机器准入仍为 **10/12（83.3%）**。企业 Registry、独立跨主机、企业 KMS/keyless、透明日志、真实事实平台、真实模型 holdout、真人可用性测试和人工发布批准仍未完成，`production_ready=false`。

## P3-32 真实迁移变化的 PostgreSQL 快照恢复（2026-11-10）

- 新增数据库回滚证据验证器，强制绑定 current / rollback 完整 Git SHA、迁移数量、迁移清单 SHA-256、证据时效和恢复保证；任一不一致均失败关闭。
- 新增一次性 PostgreSQL 16 + pgvector 快照恢复演练：应用 15 个基线迁移、写入业务探针、`pg_dump -Fc`、应用第 16 个候选 fixture 迁移、执行快照后写入，再用 `pg_restore --exit-on-error` 恢复到隔离数据库。
- 公共 schema 的表、列、约束和索引生成确定性 SHA-256；真实结果满足基线与候选不同、恢复后与基线完全一致。
- fixture 迁移只位于测试夹具目录，不进入正式迁移目录；正式迁移继续保持前向不可变，没有生成破坏性 DOWN SQL。
- rollout 在迁移清单不同时必须消费数据库恢复证据；最终记录 `database_migration_rollback_exercised=true`、`database_release_safety_verified=true` 和 `database_release_basis=snapshot_restore_rehearsed`。
- 最终候选 `501de6e53e0c3e109b1472ff175d556182629b87` 与回滚候选 `d6bff1e08451cbb9384450ad8db6f637864d4598` 完成真实 rollout：current 与 rollback 部署检查均 23/23，双 Worker 均 4/4 完成、错误 0、死信 0。
- 缺少证据、current SHA 篡改、恢复 schema 指纹篡改和过期证据四类负向验证均非零退出；负向场景没有启动或遗留临时环境。
- 专项 **33/33**、后端全量 **1442 collected / 1441 passed / 1 skipped / 0 failed**；Python、PowerShell、Bash、YAML、P3-32 JSON 和差异检查通过，相关容器、网络和卷残留为 0。
- 本机 WSL 入口不可用，因此使用已有固定 digest Linux 容器执行真实 Bash `-n`，4/4 通过；全部受版本控制 YAML 语法 9/9 通过。
- 详细说明：`docs/testing/数据库迁移快照恢复P3-32-2026-11-10.tdd.md`；结构化证据：`docs/testing/artifacts/*p3-32-2026-11-10.json`。

P3-32 本地工程与证据闭环完成度 **100%**。企业 Registry、独立跨主机、企业 KMS/keyless、透明日志和生产备份平台仍为 **0%**；单机一次性 PostgreSQL 演练不能替代生产备份加密、保留策略、权限审计和正式 RPO/RTO，当前 `release_authorized=false`、`production_ready=false`。

## P3-33 数据库恢复目标与备份治理门禁（2026-11-10）

- 新增 `database-recovery-objectives-verification-v1` 验证器，校验快照 SHA-256、非零大小、五段恢复时间线、RPO/RTO 测量一致性、目标阈值、候选 SHA、证据时效和恢复保证。
- PostgreSQL 恢复脚本新增快照哈希、大小、UTC 时间点及实测 RPO/RTO；当前快照范围明确为容器本地临时存储。
- 企业备份治理拆分为加密、密钥管理、异地副本、保留策略和恢复访问审计五项，任一缺失都保持 `enterprise_backup_governance_verified=false`。
- 统一目标准入新增第八类 `database_recovery` 证据，以及本地恢复目标和企业备份治理两项硬检查；证据 current SHA 必须与候选一致并进入准入 bundle 哈希。
- 历史 P3-32 真实证据因缺少新字段被新验证器正确拒绝，退出码 1，没有默认兼容放行。
- 首次真实演练因 Windows 保留端口失败，发现已创建容器不会清理；新增 RED `6d1d538` 并以 GREEN `eec6aa7` 修复，保留端口复验非零退出且残留容器为 0。
- RED **13 failed / 21 passed**，GREEN **35/35**，扩展专项 **40/40**；后端全量 **1449 collected / 1448 passed / 1 skipped / 0 failed**；Python、PowerShell 和差异检查通过。
- 当前虚拟环境没有 `coverage` 包，本轮覆盖率未测，没有联网改变锁定依赖。
- Docker 恢复后，最终候选 `eec6aa7a4102d68f23e7929824e2f4ecdb74e80c` 完成真实快照恢复：快照 112,882 字节，实测 RPO 2.762 秒、RTO 1.005 秒，全部本地恢复目标检查通过。
- current 与 rollback 均完成双 Worker 4/4，错误 0、死信 0；恢复 current 后实时目标探针通过，相关容器、卷和网络残留为 0。
- 14 项统一准入中 8 项通过；P3-33 本地 rollout、目标运行和数据库恢复均通过，旧 P3-31 供应链/Registry SHA、企业 Registry、跨主机和企业备份治理保持阻断。
- 详细说明：`docs/testing/数据库恢复目标与备份治理P3-33-2026-11-10.tdd.md`；结构化证据：`docs/testing/artifacts/*p3-33-2026-11-10.json`。

P3-33 本地工程与真实证据闭环完成度 **100%（5/5）**。当前候选统一机器准入为 **8/14（57.1%）**；下一阶段需要为当前 SHA 再生供应链、签名和安全 Registry 证据。企业 Registry、跨主机和企业备份治理仍为外部阻断，`release_authorized=false`、`production_ready=false`。

## P3-34 单一候选完整证据再生（2026-11-10）

- 固定运行候选 `80098eb4c948013a5c886f421f09130d6c51a48c`，在不修改运行代码的前提下重新生成供应链、签名、安全 Registry、数据库恢复、rollout、目标环境和统一准入证据。
- Linux Runner 使用固定 digest 的 Docker 27、只读源码、独立 `.tmp` 和共享 Docker socket；provenance、SPDX 2.3（198 包）、Cosign 签名和 `release_sha` 注解均通过。
- 供应链 index digest 为 `sha256:9ed12b43de3ae90b0b369218dbabce9f38da0d4334b75bca2bb351b162afd6cf`，linux/amd64 应用 manifest 为 `sha256:dcdaf81d81ac3bb560a83cfa624c6360086b067ce36d2393d972a882d9d57929`；rollout 运行镜像与该 manifest 精确一致。
- 同一候选的本地 TLS/Auth Registry 通过 CA 校验、未信任 CA 拒绝、无凭据和错误凭据拒绝、认证推送及 digest 拉取；企业 Registry 仍未验证。
- 数据库快照恢复再次实跑：快照 112,894 字节，SHA-256 为 `5d059fad33ba01f6ee47996232e10133f6d4d512fbe0f3666cf2c63dded21bfb`，实测 RPO 2.681 秒、RTO 1.072 秒，本地目标全部通过。
- 当前候选与回滚候选均完成双 Worker 4/4 队列，错误和死信为 0；当前提交/端到端 P95 为 959.81/4,072.38 ms，回滚为 1,384.91/3,555.23 ms。
- 恢复当前候选后，实时目标探针确认 `test` 环境、完整 SHA、数据库、Redis、队列、共享 checkpointer 和共享存储全部通过；单节点不冒充跨主机。
- 14 项统一准入中 11 项通过，P3-33 的 `release_sha_verified`、`image_digest_verified`、`registry_control_release_bound` 三个本地阻断项全部消除。
- 唯一阻断为企业 Registry、独立跨主机和企业备份治理；强制机器准入正确返回退出码 1，`release_authorized=false`、`production_ready=false`。
- 后端全量 **1449 collected / 1448 passed / 1 skipped / 0 failed**；Python、PowerShell 9/9、Bash 4/4、YAML 9/9、JSON 17/17 均通过，P3-34 容器、卷和网络残留为 0。
- 当前虚拟环境没有 `coverage`，本阶段未联网修改锁定依赖，覆盖率未测。
- 详细说明：`docs/testing/单一候选完整证据再生P3-34-2026-11-10.tdd.md`；结构化证据：`docs/testing/artifacts/*p3-34-2026-11-10.json`；canonical `target-release-admission.json` 已同步到本次候选。

P3-34 本地工程与完整证据再生完成度 **100%**；统一机器准入为 **11/14（78.6%）**。本地可解决的版本绑定问题已经清零，剩余三项只能由真实企业基础设施和授权验证完成，当前仍为 `production_ready=false`。

## P3-35 至 P3-37 发布报告证据与覆盖率加固（2026-11-11）

- P3-35 修复政策检索证据字段契约漂移，发布报告重新识别当前 `embedding_mode / embedding_model / embedding_stats` 契约；真实本地基准 Recall@5 为 1.0000、MRR 为 0.9375，证据仍明确为 stub 合成查询。
- P3-36 将 `coverage==7.17.2` 纳入 Linux/Windows 开发哈希锁，在本地和 GitHub CI 建立发布报告脚本 80% 语句覆盖门禁；运行时锁不含测试工具。
- P3-37 发现综合覆盖率不能替代独立分支阈值，新增结构化覆盖率验证器，分别要求语句和分支覆盖率不低于 80%；无分支数据、非法计数和低阈值均失败关闭。
- P3-37 GREEN 专项 **49/49**；`after_sales_release_report.py` 语句 **471/535（88.04%）**、分支 **107/132（81.06%）**、综合 **87%**；90% 分支负向门禁退出码 1。
- 后端全量 **1466 collected / 1465 passed / 1 skipped / 0 failed**；Python、PowerShell、YAML 与差异检查通过。
- 详细说明：`docs/testing/政策检索证据契约修复P3-35-2026-11-11.tdd.md`、`docs/testing/发布报告覆盖率门禁P3-36-2026-11-11.tdd.md`、`docs/testing/发布报告分支覆盖率门禁P3-37-2026-11-11.tdd.md`。

P3-35 至 P3-37 本地工程完成度 **100%**。覆盖率只证明关键发布报告脚本的测试执行范围，不代表全仓质量、Agent 准确率或生产就绪；当前仍为 `production_ready=false`。


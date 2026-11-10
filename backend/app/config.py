"""全局配置。

设计原则（对应设计文档 11 节）：
- 模型密钥只从服务端环境变量读取，禁止写入前端与普通日志。
- 所有外部依赖（PG / Redis / MinIO / 模型）都可降级，保证本地零依赖也能跑通主流程。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent


def _default_sqlite_url() -> str:
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{data_dir / 'pic.db'}"


class Settings(BaseSettings):
    """应用配置。所有字段均可用环境变量或 .env 覆盖。"""

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 基础 ----------------
    app_name: str = "电商售后工单智能处理系统"
    app_env: Literal["local", "dev", "test", "prod"] = "local"
    debug: bool = True
    api_prefix: str = "/api/v1"
    # 交互式接口文档（/docs、/redoc、/openapi.json）：**生产环境强制关闭**（见 main.py），
    # 其余环境默认开启。这些端点会把全部端点与数据结构公开给任何能访问服务的人。
    expose_docs: bool = True
    # 正式发布证据只读目录。应用只读取固定 JSON，不在请求中生成、修改或批准证据。
    delivery_evidence_dir: str = str(PROJECT_ROOT / "docs" / "testing" / "artifacts")
    delivery_evidence_max_age_hours: int = Field(default=24, ge=1, le=168)
    # 部署时注入构建提交 SHA；为空时证据看板明确显示“版本未绑定”。
    app_release_sha: str = ""

    # ---------------- 数据库 ----------------
    # 本地默认 SQLite，生产使用 postgresql+psycopg://...
    database_url: str = Field(default_factory=_default_sqlite_url)
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ---------------- 向量 ----------------
    # auto（按可用性自动选）| pgvector（Postgres）| qdrant（独立向量库）| fallback（Python 余弦）
    # auto 的判定顺序：配了 QDRANT_URL → qdrant；否则数据库是 Postgres → pgvector；否则 fallback。
    # 连不上时逐级降级，并在 /health 的 vector_store 里如实上报**实际生效**的那个。
    vector_backend: Literal["auto", "pgvector", "qdrant", "fallback"] = "auto"
    embedding_dim: int = 1024
    vector_table_name: str = "chunk_vectors"

    # ---------------- Qdrant（向量库第三档，服务端模式）----------------
    # 留空表示不使用 Qdrant（auto 会跳到 pgvector / fallback）
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection: str = "pic_chunks"
    qdrant_timeout_seconds: float = 10.0
    qdrant_prefer_grpc: bool = False
    # HNSW 搜索精度：越大越准、越慢（Qdrant 是近似检索，见 README §7.2 的取舍说明）
    qdrant_search_ef: int = 128

    # ---------------- 缓存 / 队列 ----------------
    redis_url: str = "redis://localhost:6379/0"
    # Redis 不可用时使用进程内缓存，功能不中断
    cache_backend: Literal["auto", "redis", "memory"] = "auto"
    cache_ttl_seconds: int = 300
    # Redis 不可用时的降级后端：sqlite = 落盘（重启后仍命中、多进程共享），memory = 纯进程内。
    # 默认 sqlite：开发/演示时后端会被反复重启，纯内存缓存每次重启都清零，
    # 于是"同一个问题问第二遍"也永远不命中，缓存看着像没生效。
    cache_fallback: Literal["sqlite", "memory"] = "sqlite"
    # 降级缓存落盘位置（独立于业务库：缓存写入不该进业务备份，也不需要迁移）
    cache_sqlite_path: str = str(PROJECT_ROOT / "data" / "cache.db")

    # ---------------- 向量缓存 ----------------
    # 同一段查询文本在一次诊断里会被多个 Agent 重复向量化（各自检索不同知识库），
    # 接了真实 Embedding 接口时每次都是计费调用，因此按文本缓存向量。
    embedding_cache_enabled: bool = True
    embedding_cache_ttl_seconds: int = 3600
    embedding_cache_max_entries: int = 512

    # ---------------- 对象存储 ----------------
    # local（本地目录）| minio（S3 兼容）
    storage_backend: Literal["local", "minio"] = "local"
    storage_local_dir: str = str(PROJECT_ROOT / "data" / "objects")
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "pic-documents"
    minio_secure: bool = False

    # ---------------- 认证 ----------------
    jwt_secret: str = "dev-only-secret-change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 12
    password_scheme: Literal["argon2", "pbkdf2"] = "pbkdf2"
    # 浏览器会话 Cookie。生产默认按 HTTPS 发送；本地 HTTP 保持 Secure=false，
    # 否则浏览器不会保存/发送会话，导致本地登录看似成功但页面立即掉线。
    auth_cookie_name: str = "pic_session"
    auth_csrf_cookie_name: str = "pic_csrf"
    auth_csrf_header_name: str = "X-CSRF-Token"
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    auth_cookie_secure: bool | None = None

    # ---------------- 模型网关 ----------------
    # OpenAI 兼容协议；DeepSeek / Qwen / GLM 均可通过 base_url 切换
    llm_provider: Literal["openai_compatible", "stub"] = "stub"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 2
    llm_temperature: float = 0.2
    llm_max_output_tokens: int = 4000

    # Embedding：openai_compatible（含 BGE-M3 服务化接口）| stub（离线确定性向量）
    embedding_provider: Literal["openai_compatible", "stub"] = "stub"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"
    # fallback：开发环境保持可用；error：验收/生产失败即阻断，防止真实模型掉线后
    # 静默改用哈希向量却仍被误认为“语义检索已验证”。
    embedding_failure_mode: Literal["fallback", "error"] = "fallback"

    # ---------------- 多模型路由（按 Agent / 节点覆盖模型） ----------------
    # 形如 {"report_agent": {"model": "deepseek-reasoner"},
    #        "feedback_agent": {"model": "qwen-max",
    #                           "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    #                           "api_key": "sk-xxx"},
    #        "intent_classifier": {"model": "qwen-turbo"}}
    # 未列出的 Agent 自动落回全局 LLM_* 配置；密钥只从服务端环境变量读取，绝不进日志与前端。
    agent_model_overrides: dict[str, dict] = {}

    # 主模型失败时的备用模型（沿用主模型的端点/密钥；跨供应商请在 agent_model_overrides 里显式配 base_url）。
    # 顺序即降级顺序：主模型 → 备用 1 → 备用 2 → … → 确定性归纳。
    llm_fallback_models: list[str] = []

    # ---------------- 模型灰度（阶段六：模型路由 + 灰度） ----------------
    # 形如 {"report_agent": {"model": "deepseek-reasoner-v2", "percent": 10,
    #                        "base_url": "https://api.deepseek.com/v1", "api_key": "sk-xxx"}}
    # `percent` 是**任务级**灰度比例：按 task_id 稳定哈希分桶，同一个任务要么整体走灰度模型、
    # 要么整体走对照模型（不会一个任务里两个 Agent 各走一半），这样前后对比才有意义。
    # 未命中灰度的任务完全按 agent_model_overrides / 全局配置执行。
    model_canary: dict[str, dict] = {}
    # 灰度分桶盐值：改变它会重新分配分桶（换一批任务进灰度），排查问题时可用来快速换样。
    model_canary_salt: str = "pic-canary-1"
    # 灰度效果对比所需的最小样本（每侧任务数）：不足时实验接口只报"样本不足"，不给胜负结论。
    # 为什么要有这个门槛：几个任务的差异基本是噪声，用噪声做选型决策比不做更危险。
    canary_min_sample: int = 5

    # ---------------- 指标 / 数据集级密级（access_policies 的实际判定入口） ----------------
    # 受限指标域：{域: [关键词]}。问题文本命中关键词即视为"请求该域的指标"，
    # 再按 access_policies 里 resource_type=metric_domain、resource_id=域 的策略判定是否放行。
    # 为什么用关键词而不是让模型判断：这是权限闸门，必须可解释、可测试、可复现。
    # ⚠️ 关键词要**精挑**：写"收入"会连"付费会员收入"这类普通问题一起拦掉，
    # 所以这里只放明确的经营/财务口径词，租户可按自己的口径扩充。
    restricted_metric_domains: dict[str, list[str]] = {
        "finance": [
            "营收",
            "GMV",
            "gmv",
            "收入分成",
            "经营大盘",
            "利润",
            "毛利",
            "财务报表",
            "财务数据",
            "财务明细",
        ]
    }

    # ---------------- 成本与预算（阶段六：成本控制） ----------------
    # 计费表：模型名 -> {"input": 每百万输入 token 单价, "output": 每百万输出 token 单价,
    #                    "cache_hit_input": 命中缓存的输入单价（可选）}
    # ⚠️ 下面的数字只是**示例默认值**，供应商会调价，请以控制台实际价格为准后覆盖本项。
    model_prices: dict[str, dict] = {
        "deepseek-chat": {"input": 2.0, "output": 8.0, "cache_hit_input": 0.5},
        "deepseek-reasoner": {"input": 4.0, "output": 16.0, "cache_hit_input": 1.0},
    }
    # 未在上表登记的模型用这组单价兜底（宁可高估，也不要漏算成本）
    model_price_default: dict = {"input": 4.0, "output": 16.0, "cache_hit_input": 1.0}
    currency: str = "CNY"
    # 单任务 token 上限；0 = 不限制（比金额更直观，适合先卡住"失控放大"）
    task_token_limit: int = 0
    # 单任务成本上限（元）；0 = 不限制。超出后按 budget_exceeded_action 处理。
    task_cost_limit: float = 0.0
    # 单租户单日成本上限（元）；0 = 不限制
    tenant_daily_cost_limit: float = 0.0
    # 超预算后的动作：degrade=停止调用模型、继续用确定性归纳出报告；abort=直接失败可重试
    budget_exceeded_action: Literal["degrade", "abort"] = "degrade"

    # ---------------- 并发与限流（阶段六：稳定性） ----------------
    # 全局模型调用并发上限（多个 Agent 并行时避免把供应商打到限流）
    model_max_concurrency: int = 4
    # 等待并发额度的最长时间（秒）；超时按模型调用失败处理并走降级链
    model_concurrency_timeout_seconds: float = 30.0
    # 单个用户每分钟最多创建多少个诊断任务；0 = 不限制
    task_submit_rate_limit_per_minute: int = 20
    # 单个「直连 IP + 用户名」每分钟最多尝试登录次数；0 = 不限制（生产禁止关闭）
    login_attempt_rate_limit_per_minute: int = Field(default=10, ge=0)

    # ---------------- 缓存（阶段六：缓存与降级） ----------------
    # 检索结果缓存 TTL（秒）；0 = 关闭。
    # 默认放到 30 分钟：检索是这里最贵的一步（无 pgvector 时要把候选切片的 1024 维向量
    # 整体读回 Python 算余弦），而 5 分钟的 TTL 会让"重启后端再问同一个问题"大多落空
    # —— 降级缓存虽然落盘，却因为条目过期而照样未命中。
    # 之所以敢放长：数据变化由**数据指纹**失效，代码逻辑变化由 `RETRIEVAL_LOGIC_VERSION`
    # 失效，换 embedding 模型由向量模型身份失效，因此 TTL 只用来兜底"缓存体积"，
    # 不再承担"保证结果新鲜"的职责。
    retrieval_cache_ttl_seconds: int = 1800
    # 指标查询缓存 TTL（秒）；0 = 关闭。
    # 保持 5 分钟：指标查询本身很轻（一条 SQL 聚合），没必要为它长期占缓存。
    metric_cache_ttl_seconds: int = 300

    # ---------------- 任务执行与断点恢复（多副本生产化） ----------------
    # 任务分发方式：
    #   inline     —— 建任务后同步执行（测试/单机调试），接口会阻塞到工作流结束
    #   threadpool —— 进程内线程池（默认，单副本够用）
    #   queue      —— 写入任务队列，由 worker 进程领取执行（多副本安全，见 app/worker.py）
    task_execution_backend: Literal["inline", "threadpool", "queue"] = "threadpool"
    # queue 模式下是否在 API 进程内顺带跑一个 worker（本地开发方便；生产应为 false，单独跑 worker）
    worker_embedded: bool = True
    # worker 并发领取的任务数
    worker_concurrency: int = 2
    # 空闲时的轮询间隔（秒）
    worker_poll_interval_seconds: float = 1.0
    # 任务租约（秒）：worker 崩了以后，超过这个时间未被确认的任务会被重新放回队列
    worker_lease_seconds: int = 600
    # 单个任务最多被领取执行几次（含首次），超过则标记为失败，避免坏任务无限重试
    worker_max_attempts: int = 3
    # worker 标识：默认取「主机名-PID」，便于在队列里看出是谁在处理
    worker_id: str = ""
    # 独立 Worker 容器活性探针：后台线程定期刷新文件，Docker 只读取时间戳。
    # 默认落在项目 data 目录以兼容本机运行；容器编排覆盖到可写的 /tmp。
    worker_heartbeat_path: str = str(PROJECT_ROOT / "data" / "worker-heartbeat")
    worker_heartbeat_interval_seconds: float = Field(default=5.0, gt=0, le=60)
    worker_heartbeat_max_age_seconds: float = Field(default=20.0, gt=1, le=300)

    # ---------------- 运行告警外部通知 ----------------
    # 默认关闭。启用后只把告警写入持久发送队列，由后台发送线程异步投递，
    # 不在业务事务里直接访问外部网络。
    alert_webhook_enabled: bool = False
    alert_webhook_url: str = ""
    # 可选 HMAC-SHA256 签名密钥；仅从服务端环境读取，不进入健康检查、日志或载荷。
    alert_webhook_secret: str = ""
    alert_webhook_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    alert_webhook_max_attempts: int = Field(default=5, ge=1, le=20)
    alert_webhook_retry_base_seconds: float = Field(default=10.0, ge=0, le=3600)
    alert_webhook_poll_interval_seconds: float = Field(default=2.0, gt=0, le=60)
    alert_webhook_lease_seconds: int = Field(default=30, ge=5, le=600)

    # ---------------- 只读主动异常触发器 ----------------
    # 默认关闭。扫描只读取标准化指标并创建候选告警，不调用模型、不派发 Agent、
    # 不访问外部网络；必须由管理员明确确认后才创建并派发诊断任务。
    proactive_anomaly_enabled: bool = False
    proactive_anomaly_metrics: list[str] = []
    proactive_anomaly_lookback_days: int = Field(default=30, ge=3, le=365)
    # 最新指标日期距离 UTC 今天的最大允许天数；超期或未来日期都不生成候选告警。
    proactive_anomaly_max_data_age_days: int = Field(default=2, ge=0, le=365)
    # 指标级覆盖，例如 {"crash_rate": 0, "next_day_retention": 2}；未配置时用全局值。
    proactive_anomaly_max_data_age_by_metric: dict[str, int] = {}
    proactive_anomaly_method: Literal["zscore", "iqr"] = "zscore"
    proactive_anomaly_threshold: float = Field(default=2.5, gt=0, le=10)
    # 独立调度进程轮询间隔与数据库租约；API 进程不会启动该循环。
    proactive_anomaly_interval_seconds: int = Field(default=300, ge=30, le=86400)
    proactive_anomaly_lease_seconds: int = Field(default=900, ge=60, le=86400)

    # ---------------- 标准化只读指标源 ----------------
    # 默认关闭。启用后由独立主动调度器在扫描前通过 GET 拉取白名单指标，
    # 再复用现有导入校验写入本地快照；不会向上游执行任何写操作。
    metric_source_http_enabled: bool = False
    metric_source_http_url: str = ""
    metric_source_http_auth_headers: dict[str, str] = {}
    metric_source_http_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    metric_source_http_max_retries: int = Field(default=2, ge=0, le=5)
    metric_source_http_retry_backoff_seconds: float = Field(default=0.2, ge=0, le=10)
    metric_source_http_max_rows: int = Field(default=50000, ge=1, le=200000)
    metric_source_http_max_response_bytes: int = Field(
        default=4 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024
    )

    # Checkpointer 后端：memory（进程内，单副本）/ sqlite（同机多进程）/ postgres（多副本）
    checkpointer_backend: Literal["memory", "sqlite", "postgres"] = "memory"
    # sqlite checkpointer 的库文件；为空时放在数据库同目录
    checkpointer_sqlite_path: str = ""
    # postgres checkpointer 的连接串；为空时用 DATABASE_URL
    checkpointer_postgres_url: str = ""

    # ---------------- 工作流 / Agent 受控参数 ----------------
    agent_timeout_seconds: int = 90
    agent_max_retries: int = 1
    agent_max_tokens: int = 6000
    # 单个 Agent 一次运行内允许的工具调用次数（超出即拒绝并记审计）
    agent_max_tool_calls: int = 6
    workflow_max_retrieve_rounds: int = 2
    # 有界反思默认关闭；开发/灰度环境显式开启。0 表示只执行原有代码门禁。
    reflection_enabled: bool = False
    reflection_ticket_enabled: bool = True
    reflection_report_enabled: bool = True
    # 反思能力灰度：服务端按 task_id 稳定分桶，请求体不能覆盖该判定。
    # 空作用域表示全部租户/风险/问题类型；百分比为 0 时保留对照组，100 时全量启用。
    reflection_rollout_percent: int = Field(default=100, ge=0, le=100)
    reflection_rollout_salt: str = "reflection-rollout-v1"
    reflection_rollout_tenants: list[str] = []
    reflection_rollout_risk_levels: list[str] = []
    reflection_rollout_issue_types: list[str] = []
    # 发布门禁：扩大需管理员确认；安全硬指标失败时允许自动回滚到 0%。
    reflection_rollout_auto_guard: bool = True
    reflection_release_min_sample: int = Field(default=20, ge=1, le=100000)
    reflection_release_error_rate_limit: float = Field(default=0.05, ge=0, le=1)
    reflection_release_bounded_termination_min: float = Field(default=0.98, ge=0, le=1)
    reflection_release_latency_multiplier: float = Field(default=1.5, ge=1, le=10)
    reflection_release_token_multiplier: float = Field(default=1.5, ge=1, le=10)
    max_reflection_rounds: int = Field(default=1, ge=0, le=2)
    reflection_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    reflection_max_tokens: int = Field(default=1800, ge=256, le=6000)
    reflection_cost_limit: float = Field(default=0.0, ge=0)
    workflow_version: str = "wf-1.0.0"
    prompt_version: str = "prompt-1.0.0"
    embedding_version: str = "emb-1.0.0"
    index_version: str = "index-1.0.0"
    # 报告最多引用的证据条数（防止报告被证据淹没）
    report_max_evidence: int = 30
    # 证据充分性：低于该相关度视为噪声，不计入证据
    evidence_min_relevance: float = 0.02
    # 可回答性闸门：问题**信息词**（已剔除"问句外壳"，见 services/answerability.py）在证据中的
    # 覆盖率低于该值时，判定"证据不足以回答"，直接把事实降级为假设并声明缺口
    # （避免用不相关材料硬答）。
    #
    # 0.32 由 `scripts/calibrate_answerability.py` 在 50 条评测用例上标定（离线 stub 检索：
    # 哈希向量 + 二元组分词）。实测对比（同一数据集、同一新口径）：
    #   0.20（旧默认）→ 46/50 通过、拒答准确率 0.636、误拒 0、漏拒 4
    #   0.25          → 47/50、0.727、误拒 0、漏拒 3
    #   0.30          → 48/50、0.909、误拒 1、漏拒 1
    #   **0.32**      → 49/50、1.000、误拒 1、漏拒 0   ← 取此点（通过率与拒答准确率同时最高）
    #
    # 取舍要说清：0.32 落在"应当可回答"与"应当拒答"用例的**重叠区**（实测 [0.278, 0.316]）内，
    # 因此它用 1 条可回答用例被误拒，换来 4 条该拒没拒被修好。若业务更怕误拒，
    # 可退到 0.26～0.278（实测 48/50、误拒 0、漏拒 1）——这是产品取向，不是数据结论。
    # 换 embedding / reranker（例如 BGE-M3）后分数分布会变，必须用同一脚本重新标定。
    answerability_min_coverage: float = 0.32
    # 逃生口：存在明显高相关证据时，即使关键词覆盖率低也认为可回答。
    # 标定结果显示它在 0.26 以上对判定的影响可忽略（应拒答用例的最高相关度仅 0.254），
    # 保留它是为了覆盖"问法被改写但明显相关"的情形。
    answerability_high_score: float = 0.26
    # 冲突判定：同一事实下的反向表述相似度阈值
    conflict_similarity_threshold: float = 0.35

    # 触发人工审核的阈值
    risk_score_high_threshold: float = 0.7
    confidence_low_threshold: float = 0.6
    min_evidence_count: int = 3

    # ---------------- 电商售后工单（需求书 §3.3 / §4.3 / §11） ----------------
    # 风险分级的金额线。需求书把"哪些金额和动作需要主管审批"列为**开放问题**，
    # 所以这里是可配置项，而不是写死在代码里的数字；默认值是 MVP 假设，上线前须按真实政策确认。
    #   ≥ ticket_l2_amount_limit：赔付达到"客服确认线"（L2）；未达且非写操作 → L1 可自动出草稿
    #   ≥ ticket_l3_amount_limit：需主管审批（L3）
    #   ≥ ticket_l4_amount_limit：转风控，禁止自动执行（L4）
    # 注意：**写操作类动作（退款/补发/换货/赔付/优惠券）不看金额线，一律 ≥ L2**，
    # 金额线只用来决定"非写操作、但回复里承诺了补偿"这类情况的档位。
    ticket_l2_amount_limit: float = 100.0
    ticket_l3_amount_limit: float = 500.0
    ticket_l4_amount_limit: float = 5000.0
    # 事实新鲜度：订单/物流事实超过该时长视为"可能过期"，需重新拉取（§4.3 接口过期进人工）
    ticket_fact_ttl_seconds: int = 900
    # 单张工单的分析超时（§8 非功能需求：分析 P95 ≤ 20 秒，不含人工等待）
    ticket_analysis_timeout_seconds: int = 60
    # 模拟适配器开关：MVP 用模拟数据，界面与审计里必须能看出（§1 数据说明）
    ticket_use_mock_adapters: bool = True
    # 关闭 mock 后，四个端点必须全部配置，且 URL 显式包含 {order_id}。
    # 上游响应须使用标准契约：{"data": {...}, "observed_at": "ISO-8601", "request_id": "..."}。
    ticket_fact_order_url: str = ""
    ticket_fact_payment_url: str = ""
    ticket_fact_logistics_url: str = ""
    ticket_fact_aftersale_url: str = ""
    # JSON 对象，仅从服务端环境变量读取，例如 {"Authorization":"Bearer ..."}；不会出现在健康检查。
    ticket_fact_auth_headers: dict[str, str] = {}
    ticket_fact_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    ticket_fact_max_retries: int = Field(default=2, ge=0, le=5)
    ticket_fact_retry_backoff_seconds: float = Field(default=0.2, ge=0, le=5)
    # MCP 只读适配器基础层默认关闭。真实传输接入前必须补齐外部服务契约；健康检查不回显地址或鉴权头。
    ticket_fact_mcp_enabled: bool = False
    ticket_fact_mcp_endpoint: str = ""
    ticket_fact_mcp_max_response_bytes: int = Field(default=262144, ge=1024, le=1048576)
    # 单次分析最多允许升级到多 Agent 全流程的工单比例（成本闸门；见 README 成本章节）
    ticket_multi_agent_daily_budget: int = 200

    # ---------------- 安全 ----------------
    max_upload_mb: int = 20
    allowed_upload_extensions: str = ".pdf,.docx,.xlsx,.csv,.md,.txt,.json"
    # 前端来源白名单（逗号分隔）；默认放开本地开发端口
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173"
    # Uvicorn 可信代理 IP / CIDR（逗号分隔）。空值=不采信任何 X-Forwarded-*；生产禁止 *。
    # 该变量同时由 Uvicorn 自身读取，必须在进程启动前通过环境变量注入。
    forwarded_allow_ips: str = ""
    # 命中即判定为提示词注入，进入人工审核并记审计
    prompt_injection_patterns: str = (
        "忽略以上指令,忽略之前的指令,ignore previous instructions,"
        "ignore all previous instructions,system prompt,reveal your prompt,"
        "导出全部数据,绕过权限,忽略安全规则"
    )
    sensitive_output_patterns: str = "api_key,secret,password,token"

    # ---------------- 指标导入 ----------------
    metric_import_max_rows: int = 200000
    # 比例类指标的合理上界，超过则记质量标记（不直接拒绝，交由人工判断）
    metric_ratio_bounds: float = 1.5
    # 演示数据目录（播种脚本与评测使用）
    demo_data_dir: str = str(BACKEND_DIR / "data" / "demo")

    # ---------------- 检索 ----------------
    retrieval_top_k: int = 8
    retrieval_candidate_k: int = 50
    # 召回的绝对相关度下限：低于该分数的切片直接丢弃（避免"总能命中 top_k"）
    retrieval_min_score: float = 0.05
    chunk_size: int = 600
    chunk_overlap: int = 80
    # 可选 BGE-Reranker HTTP 服务；为空时使用启发式加权排序
    reranker_endpoint: str = ""
    reranker_api_key: str = ""
    reranker_timeout_seconds: int = 20
    reranker_failure_mode: Literal["fallback", "error"] = "fallback"

    # ---------------- 任务执行 ----------------
    task_worker_threads: int = 4
    # 任务轮询/中断检测间隔（秒）
    task_poll_interval_seconds: float = 0.5
    # 同步执行工作流（测试与调试用）；默认提交到后台线程池，接口立即返回
    task_run_inline: bool = False
    # 启动时自动建表并按需播种演示数据（本地零依赖模式）
    auto_init_db: bool = True
    auto_seed: bool = True
    # 启动时把上次进程中断遗留的 running/pending 任务标记为失败可重试
    # （工作流跑在进程内线程池里，进程退出后没人能推进它）
    reconcile_stale_tasks: bool = True
    # 前端构建产物目录：存在时会挂到根路径 "/"（同源访问，免 CORS）
    frontend_dist_dir: str = str(PROJECT_ROOT / "frontend" / "dist")

    @property
    def allowed_extensions(self) -> list[str]:
        return [e.strip() for e in self.allowed_upload_extensions.split(",") if e.strip()]

    @property
    def injection_patterns(self) -> list[str]:
        return [p.strip().lower() for p in self.prompt_injection_patterns.split(",") if p.strip()]

    @property
    def sensitive_patterns(self) -> list[str]:
        return [p.strip().lower() for p in self.sensitive_output_patterns.split(",") if p.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def auth_cookie_secure_effective(self) -> bool:
        """生产默认只允许 HTTPS；本地可通过配置显式覆盖。"""

        return self.app_env == "prod" if self.auth_cookie_secure is None else self.auth_cookie_secure


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

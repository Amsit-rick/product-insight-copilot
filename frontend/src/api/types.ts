/**
 * 与后端 Pydantic Schema 一一对应的 TypeScript 类型。
 * 只保留前端真正会用到的字段，字段名与后端保持一致（避免"看着像但不是那个字段"）。
 */

export type TaskStatus =
  | 'pending'
  | 'running'
  | 'waiting_human'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type RiskLevel = 'low' | 'medium' | 'high'
export type ApprovalStatus = 'not_required' | 'pending' | 'approved' | 'rejected' | 'modified'
export type StatementKind = 'fact' | 'inference' | 'hypothesis' | 'recommendation'

export interface UserOut {
  id: string
  username: string
  full_name?: string | null
  email?: string | null
  department?: string | null
  tenant_id: string
  security_level: number
  roles: string[]
}

export interface MeOut extends UserOut {
  actions: string[]
  knowledge_bases: string[]
  csrf_cookie_name: string
  csrf_header_name: string
}

export interface SessionResponse {
  expires_in: number
  user: UserOut
  csrf_cookie_name: string
  csrf_header_name: string
}

export interface PageMeta {
  total: number
  page: number
  page_size: number
}

export interface ListResponse<T> {
  items: T[]
  meta: PageMeta
}

export interface TaskOut {
  id: string
  tenant_id: string
  user_id: string
  title?: string | null
  query: string
  status: TaskStatus
  current_node?: string | null
  current_phase?: string | null
  checkpoint_version: number
  phase_attempt: number
  resume_count: number
  resumed_from_checkpoint: boolean
  wake_reason?: string | null
  state_revision: number
  intent?: string | null
  plan: Array<Record<string, unknown>>
  filters: Record<string, unknown>
  risk_level: RiskLevel
  retry_count: number
  workflow_version?: string | null
  prompt_version?: string | null
  waiting_reason?: string | null
  idempotency_key?: string | null
  evidence_count: number
  report_id?: string | null
  error?: { code?: string; message?: string } | null
  started_at?: string | null
  finished_at?: string | null
  created_at: string
  updated_at: string
}

export interface TaskEventOut {
  id: string
  task_id: string
  node?: string | null
  level: 'info' | 'warn' | 'error' | string
  message: string
  payload: Record<string, unknown>
  created_at: string
}

export interface TaskLiveEvent extends TaskEventOut {
  event_seq: number
  phase: string
}

export interface TaskExecutionUnit {
  id: string
  task_id: string
  parent_unit_id?: string | null
  name: string
  unit_type: 'workflow' | 'agent' | 'tool' | 'approval' | 'background'
  owner_type: 'workflow' | 'agent_run' | 'tool_call' | 'approval' | 'system'
  owner_ref_id: string
  status: string
  execution_mode: 'attached' | 'background'
  cancellation_policy: 'cascade' | 'independent'
  failure_policy: 'continue' | 'block_parent' | 'fail_parent'
  metadata: Record<string, unknown>
  started_at?: string | null
  finished_at?: string | null
  created_at?: string | null
  updated_at?: string | null
  children: TaskExecutionUnit[]
}

export interface TaskLiveSnapshot {
  schema_version: 'task-live.v1'
  task: TaskOut
  state_revision: number
  event_cursor?: string | null
  event_count: number
  events: TaskLiveEvent[]
  task_tree: TaskExecutionUnit
  agent_runs: Array<{
    id: string
    agent_code: string
    node?: string | null
    phase: string
    status: string
    attempt: number
    latency_ms?: number | null
    created_at?: string | null
  }>
  tool_calls: Array<{
    id: string
    tool_name: string
    status: string
    policy_decision?: string | null
    replay_policy?: 'safe' | 'idempotent' | 'unsafe' | null
    side_effect_level?: 'read_only' | 'reversible' | 'irreversible' | null
    replayed: boolean
    result_summary?: string | null
    created_at?: string | null
  }>
}

export interface AgentRunOut {
  id: string
  agent_code: string
  node?: string | null
  status: string
  attempt: number
  model?: string | null
  prompt_version?: string | null
  latency_ms?: number | null
  total_tokens?: number | null
  error?: string | null
  summary?: string | null
  findings: number
  /** 本次运行是否真的用到了模型输出（false = 已降级为确定性归纳） */
  model_used?: boolean
  /** 路由来源：canary（灰度）/ override（按 Agent 覆盖）/ global（全局默认） */
  model_source?: string | null
  /** 灰度分桶号（0-99）：用来判断这次任务走了灰度还是对照 */
  canary_bucket?: number | null
}

export interface ToolCallOut {
  id: string
  tool_name: string
  status: string
  policy_decision?: string | null
  latency_ms?: number | null
  arguments: Record<string, unknown>
  result_summary?: string | null
  replay_policy?: 'safe' | 'idempotent' | 'unsafe' | null
  side_effect_level?: 'read_only' | 'reversible' | 'irreversible' | null
  idempotency_key?: string | null
  replayed?: boolean
  approval_reference?: string | null
}

export interface TaskTrace {
  task_id: string
  status: TaskStatus
  risk_level: RiskLevel
  retry_count: number
  agent_runs: AgentRunOut[]
  tool_calls: ToolCallOut[]
  totals: { tokens: number; latency_ms: number; tool_calls: number; denied_tool_calls: number }
  /** 成本明细（按模型聚合；单价表见后端 MODEL_PRICES） */
  cost?: {
    currency: string
    input_tokens: number
    output_tokens: number
    cache_hit_tokens: number
    total_tokens: number
    amount: number
    calls: number
    per_model: Record<
      string,
      { calls: number; input_tokens: number; output_tokens: number; amount: number }
    >
    unpriced_models: string[]
  }
  /** 预算状态：是否触及 token/成本上限，以及超限后的动作 */
  budget?: {
    exceeded: boolean
    reasons: string[]
    tokens: number
    amount: number
    currency: string
    action: 'degrade' | 'abort' | string
    limits: Record<string, number>
  }
}

export interface CostSummary {
  window_days: number
  cost: {
    currency: string
    input_tokens: number
    output_tokens: number
    cache_hit_tokens: number
    total_tokens: number
    amount: number
    calls: number
    per_model: Record<string, { calls: number; amount: number }>
    unpriced_models: string[]
  }
  tasks_per_day: Record<string, number>
  daily_limit: number
  today: { exceeded: boolean; amount: number; limit: number; currency?: string }
}

/** 灰度效果对比中的单侧统计（灰度侧 / 对照侧） */
export interface CanaryGroupStats {
  label: string
  task_count: number
  run_count: number
  model_calls: number
  model_used_rate: number
  schema_ok_rate: number
  error_rate: number
  avg_findings: number
  latency_p50_ms: number
  latency_p95_ms: number
  total_tokens: number
  total_cost: number
  cost_per_task: number
  report_count: number
  avg_facts_per_report: number
  cited_fact_rate: number
  conflict_rate: number
  human_review_rate: number
  avg_confidence: number
  models: string[]
}

/** 灰度 vs 对照的对比结果（含样本量门槛与逐指标结论） */
export interface CanaryExperiment {
  window_days: number
  agent_code?: string | null
  canary_configured: boolean
  canary_config: Record<string, unknown>
  /** 每侧最少任务数；不足时只给"样本不足"，不给胜负结论 */
  min_sample: number
  groups: { canary: CanaryGroupStats; control: CanaryGroupStats }
  metrics: CanaryMetricComparison[]
  report_metrics: CanaryMetricComparison[]
  verdict: '灰度更优' | '灰度更差' | '暂无明显差异' | '样本不足' | string
  verdict_reason: string
  method: { grouping: string; rates: string; means: string; limitations: string[] }
}

export interface CanaryMetricComparison {
  metric: string
  canary: number
  control: number
  delta: number
  /** 比率类指标的差异用百分点表示 */
  delta_pp?: number
  delta_pct: number
  test: 'two-proportion-z' | 'relative-delta' | string
  p_value: number | null
  verdict: string
  note: string
}

export interface ReflectionReleaseReport {
  state: {
    id: string
    status: 'active' | 'paused' | 'rolled_back' | string
    current_percent: number
    recommended_percent?: number | null
    recommendation: 'hold' | 'promote' | 'pause' | 'rollback' | string
    reason?: string | null
    version: number
    last_evaluated_at?: string | null
  }
  decision: {
    recommendation: 'hold' | 'promote' | 'pause' | 'rollback' | string
    current_percent: number
    recommended_percent: number
    reason: string
    requires_human_approval: boolean
    auto_apply: boolean
    metrics: {
      treatment_sample: number
      control_sample: number
      treatment_error_rate: number
      control_error_rate: number
      safety_violation_rate: number
      protected_field_violation_rate: number
      bounded_termination_rate: number
      treatment_p95_latency_ms: number
      control_p95_latency_ms: number
      treatment_avg_tokens: number
      control_avg_tokens: number
      treatment_human_review_rate: number
      control_human_review_rate: number
    }
  }
  scope: { tenants: string[]; risk_levels: string[]; issue_types: string[] }
  config: {
    global_enabled: boolean
    ticket_enabled: boolean
    report_enabled: boolean
    configured_percent: number
    min_sample: number
    auto_guard: boolean
  }
  window_days: number
}

export interface DeliveryReadinessSnapshot {
  schema_version: 'delivery-readiness-snapshot-v1'
  status: 'missing' | 'invalid' | 'stale' | 'version_unbound' | 'commit_mismatch' | 'incomplete' | 'current' | string
  evidence_valid: boolean
  evidence_bundle_verified: boolean
  evidence_fresh: boolean
  release_sha_verified: boolean | null
  trusted_production_ready: boolean
  reasons: string[]
  generated_at?: string | null
  age_hours?: number | null
  evidence_commit_sha?: string | null
  evidence_manifest_generated_at?: string | null
  decision?: string | null
  machine_gates_ready: boolean
  release_decision_status: string
  local_demo_ready: boolean
  candidate_ready: boolean
  declared_production_ready: boolean
  external_gates: Array<{
    key: string
    label: string
    status: 'verified_external' | 'blocked_external' | 'not_reported' | string
    required: string
  }>
  release_checks: Array<{ name: string; status: string; detail: string }>
  candidate_blockers: string[]
}

export interface OperationalAlert {
  id: string
  tenant_id: string
  kind: string
  severity: 'info' | 'warning' | 'critical' | string
  title: string
  message: string
  status: 'open' | 'acknowledged' | 'resolved' | string
  source_type?: string | null
  source_id?: string | null
  occurrence_count: number
  first_occurred_at: string
  last_occurred_at: string
  acknowledged_by?: string | null
  acknowledged_at?: string | null
  resolved_by?: string | null
  resolved_at?: string | null
  metadata: Record<string, unknown>
}

export interface OperationalAlertList {
  items: OperationalAlert[]
  summary: { open: number; acknowledged: number; critical: number }
}

export interface ProactiveTriggerStatus {
  enabled: boolean
  read_only_scan: boolean
  metrics: string[]
  lookback_days: number
  max_data_age_days: number
  max_data_age_by_metric: Record<string, number>
  metric_source: {
    enabled: boolean
    configured: boolean
    upstream_read_only: boolean
  }
  method: 'zscore' | 'iqr'
  threshold: number
  automatic_task_dispatch: boolean
  requires_human_confirmation: boolean
  scheduler?: {
    running: boolean
    heartbeat_at?: string | null
    last_run_started_at?: string | null
    last_run_finished_at?: string | null
    last_status?: 'completed' | 'partial' | 'stopped' | 'lease_lost' | null
    has_error: boolean
    last_result: {
      tenants_total?: number
      tenants_succeeded?: number
      tenants_failed?: number
      metrics_scanned?: number
      metrics_skipped?: number
      skipped_reasons?: Record<string, number>
      anomalies_found?: number
      source_rows_received?: number
      source_rows_written?: number
      source_sync_failures?: number
    }
  } | null
}

export interface ProactiveScanResult extends ProactiveTriggerStatus {
  read_only: boolean
  metrics_scanned: number
  anomalies_found: number
  alert_ids: string[]
  skipped: Array<{
    metric_name: string
    reason: string
    latest_stat_date?: string
    age_days?: number
    max_age_days?: number
  }>
}

export interface ProactiveDiagnosisResult {
  created: boolean
  task: TaskOut
  alert: OperationalAlert
}

export interface OperationalAlertDelivery {
  id: string
  alert_id: string
  tenant_id: string
  channel: 'webhook' | string
  occurrence_no: number
  status: 'pending' | 'claimed' | 'sent' | 'dead' | string
  attempts: number
  max_attempts: number
  visible_at?: string | null
  last_error?: string | null
  last_status_code?: number | null
  delivered_at?: string | null
  created_at?: string | null
}

export interface OperationalAlertDeliveryList {
  items: OperationalAlertDelivery[]
  summary: { pending: number; claimed: number; sent: number; dead: number }
}

/** 访问策略中的细粒度条件（与后端 conditions 字段一一对应） */
export interface AccessPolicyConditions {
  min_clearance?: number
  valid_from?: string
  valid_until?: string
  timezone?: string
  time_window?: { start?: string; end?: string; days?: number[]; timezone?: string }
  row_filters?: Record<string, string[]>
  departments?: string[]
  risk_tiers?: string[]
  classifications?: string[]
  min_amount?: number
  max_amount?: number
  tool_names?: string[]
}

export interface AccessPolicyOut {
  id: string
  subject_type: 'role' | 'user' | string
  subject_id: string
  resource_type: 'metric' | 'metric_domain' | 'dataset' | string
  resource_id: string
  effect: 'allow' | 'deny' | 'require_approval' | string
  actions: string[]
  conditions: AccessPolicyConditions
  created_at?: string | null
}

export interface AccessPolicyCreate {
  subject_type: string
  subject_id: string
  resource_type: string
  resource_id: string
  effect: string
  actions: string[]
  conditions: AccessPolicyConditions
}

/** 一条策略对某个主体的判定结论（权限预演逐条展示） */
export interface PolicyVerdict {
  policy_id: string
  resource_type: string
  resource_id: string
  subject: string
  effect: 'allow' | 'deny' | string
  // 机器可读结论：allowed / subject_mismatch / clearance_insufficient / explicit_deny / condition_unmet
  code: string
  label: string
  detail: string
  row_filters: Record<string, string[]>
}

/** 权限预演结果：结论 + 判定过程 + 生效的行级范围 */
export interface PolicySimulation {
  subject: {
    username?: string | null
    user_id?: string | null
    roles: string[]
    clearance?: number | null
    source: 'account' | 'explicit' | string
  }
  resource: { type: string; name: string }
  requested: Record<string, string>
  request_violation?: string | null
  allowed: boolean
  reason: string
  resource_type: string
  resource_name: string
  matched_policy_id?: string | null
  has_policies: boolean
  allowed_policy_ids: string[]
  effective_row_filters: Record<string, string[]>
  verdicts: PolicyVerdict[]
}

/** 统一授权预演：RBAC、租户边界与资源级 ABAC 的最终结论。 */
export interface AuthorizationSimulation {
  subject: {
    username?: string | null
    user_id?: string | null
    roles: string[]
    clearance: number
    department?: string | null
    source: 'account' | 'explicit' | string
  }
  resource: { type: string; id: string; tenant_id?: string | null }
  action: string
  decision: 'allow' | 'deny' | 'require_approval'
  allowed: boolean
  requires_approval: boolean
  reason: string
  matched_policy_ids: string[]
  effective_scope: Record<string, unknown>
}

/** 队列概览计数（/health 与队列运维页共用） */
export interface QueueStats {
  pending: number
  claimed: number
  done: number
  dead: number
  oldest_pending_at?: string | null
}

/** 一条队列项（运维视角：死了哪些、为什么死的、被重放过几次） */
export interface QueueItem {
  id: string
  task_id: string
  kind: string
  status: 'pending' | 'claimed' | 'done' | 'dead' | string
  attempts: number
  max_attempts: number
  last_error?: string | null
  claimed_by?: string | null
  finished_at?: string | null
  created_at?: string | null
  replays: number
  /** 关联任务的状态与问题（任务行不存在时为 null / true） */
  task_status?: string | null
  task_query?: string | null
  task_missing?: boolean
}

export interface QueueItemsResponse {
  stats: QueueStats
  items: QueueItem[]
}

/** 知识库问答的一条引用（可定位到文档/页码/原文片段） */
export interface QaCitation {
  index: number
  chunk_id: string
  document_id: string
  kb_code: string
  source_name: string
  page?: number | null
  document_version: string
  score: number
  snippet: string
}

/** 知识库问答结果（检索即答） */
export interface QaAnswer {
  query: string
  answer: string
  points: string[]
  citations: QaCitation[]
  insufficient: boolean
  missing: string[]
  mode: 'extractive' | 'model' | string
  knowledge_bases: string[]
  latency_ms: number
  model?: string | null
  warnings: string[]
  coverage?: Record<string, unknown>
  disclaimer: string
  conversation_id?: string
  message_id?: string
  created_at?: string
}

export interface QaHistoryItem {
  id: string
  conversation_id?: string
  query: string
  answer: QaAnswer
  created_at: string
}

export interface QaConversation {
  id: string
  title: string
  message_count: number
  last_query: string
  created_at: string
  updated_at: string
}

export interface QaConversationPage {
  items: QaConversation[]
  total: number
  page: number
  page_size: number
}

export interface QaConversationMessage {
  id: string
  conversation_id: string
  sequence: number
  query: string
  answer: QaAnswer
  created_at: string
}

export interface QaConversationMessagesPage {
  conversation: {
    id: string
    title: string
    created_at: string
    updated_at: string
  }
  items: QaConversationMessage[]
  total: number
  page: number
  page_size: number
}

export interface QaHistoryPage {
  items: QaHistoryItem[]
  total: number
  page: number
  page_size: number
}

export interface CitationOut {
  id: string
  statement_id?: string | null
  statement_type: StatementKind | string
  source_kind: 'document' | 'metric' | string
  chunk_id?: string | null
  document_id?: string | null
  kb_code?: string | null
  source_name?: string | null
  page?: number | null
  document_version?: string | null
  content?: string | null
  relevance_score?: number | null
  metric_ref?: {
    metric_name?: string | null
    capability?: string | null
    params?: Record<string, unknown> | null
    summary?: Record<string, unknown> | null
    dataset_version?: string | null
    source_version?: string | null
    quality_flags?: string[]
    warnings?: string[]
  } | null
  permission_checked: boolean
}

export interface ReportStatement {
  id?: string
  type?: StatementKind
  text: string
  evidence_refs?: string[]
  confidence?: number
  source_agent?: string | null
  basis?: string | null
  verify_by?: string | null
  priority?: string | null
  owner_role?: string | null
}

export interface ReportEvidence {
  evidence_id: string
  source_kind?: 'document' | 'metric' | string
  content?: string
  source_name?: string
  page?: number | null
  document_version?: string | null
  updated_at?: string | null
  relevance_score?: number | null
  knowledge_base?: string | null
  document_id?: string | null
  chunk_id?: string | null
  metric_name?: string | null
  capability?: string | null
  params?: Record<string, unknown> | null
  summary?: Record<string, unknown> | null
  dataset_version?: string | null
  source_version?: string | null
  quality_flags?: string[]
  warnings?: string[]
  row_count?: number | null
  agent_code?: string
  permission_checked?: boolean
}

export interface ReportConflict {
  id: string
  topic: string
  statement_a: { id: string; text: string; agent_code?: string; evidence_refs?: string[] }
  statement_b: { id: string; text: string; agent_code?: string; evidence_refs?: string[] }
  evidence_refs?: string[]
  sources?: string[]
  source_kinds?: string[]
  detected_by?: string[]
  resolution?: {
    adopted_statement?: string
    adopted_source?: string
    rejected_statement?: string
    rejected_source?: string
    priority_rule?: string
    reason?: string
    requires_human?: boolean
  } | null
}

export interface ReportOut {
  id: string
  task_id: string
  version: number
  summary?: string | null
  impact_scope: Array<{ dimension: string; value: string; evidence_refs?: string[] }>
  facts: ReportStatement[]
  inferences: ReportStatement[]
  hypotheses: ReportStatement[]
  evidence: ReportEvidence[]
  conflicts: ReportConflict[]
  recommendations: ReportStatement[]
  priority?: string | null
  confidence?: number | null
  needs_human_review: boolean
  risk_level: RiskLevel | string
  approval_status: ApprovalStatus | string
  content_md?: string | null
  data_gaps: string[]
  warnings: string[]
  citations: CitationOut[]
  citation_marks: Record<string, string>
  validation_problems: string[]
  reflection?: {
    enabled: boolean
    label: string
    verdict?: 'pass' | 'revise' | 'retrieve_more' | 'human_review' | null
    rounds: number
    issue_count: number
    revised: boolean
    issues: Array<{ code: string; severity: string; category: string; message: string; auto_fixable: boolean }>
    stop_reason?: string | null
  }
  created_at: string
  updated_at: string
}

export interface ApprovalOut {
  id: string
  task_id: string
  report_id?: string | null
  status: string
  action?: string | null
  reviewer_id?: string | null
  comment?: string | null
  diff?: Record<string, unknown> | null
  decided_at?: string | null
  created_at: string
  task_state_revision?: number | null
  task_query?: string | null
  task_risk_level?: RiskLevel | null
  waiting_reason?: string | null
  request?: {
    reason?: string
    risk_level?: string
    risk_reasons?: string[]
    conflicts?: ReportConflict[]
    coverage?: {
      evidence_count?: number
      fact_count?: number
      metric_available?: boolean
      data_gaps?: string[]
    }
    agent_summaries?: Record<string, { ok?: boolean; summary?: string; findings?: number; confidence?: number }>
    top_facts?: Array<{ id?: string; text: string; evidence_refs?: string[] }>
    actions?: string[]
  }
}

export interface KnowledgeBaseOut {
  id: string
  code: string
  name: string
  description?: string | null
  security_level: string
  document_count: number
  chunk_count: number
  index_status: string
  last_indexed_at?: string | null
}

export interface DocumentOut {
  id: string
  kb_id: string
  kb_code?: string | null
  name: string
  source_name: string
  source_type: string
  mime_type?: string | null
  size_bytes: number
  department?: string | null
  security_level: string
  document_version: string
  status: string
  error_message?: string | null
  doc_updated_at?: string | null
  chunk_count: number
  uploaded_by?: string | null
  created_at: string
}

export interface MetricDefinitionOut {
  name: string
  display_name: string
  unit: string
  period: string
  description?: string | null
  numerator?: string | null
  denominator?: string | null
  allowed_dimensions: string[]
  higher_is_better: boolean
  source_version?: string | null
}

export interface MetricQueryResponse {
  capability: string
  metric_name: string
  params: Record<string, unknown>
  rows: Array<Record<string, unknown>>
  summary: Record<string, unknown>
  dataset_version?: string | null
  source_version?: string | null
  quality_flags: string[]
  warnings: string[]
  available: boolean
}

export interface MetricImportResponse {
  import_id: string
  file_name: string
  dataset_version: string
  status: string
  rows_total: number
  rows_valid: number
  rows_invalid: number
  duplicates: number
  missing_days: string[]
  quality_flags: string[]
  issues: Array<{ row?: number | null; level: string; code: string; message: string }>
  metric_names: string[]
  date_range: { min?: string | null; max?: string | null }
  defined_metrics_unknown: string[]
}

export interface WorkflowInfo {
  nodes: string[]
  analysis_branches: string[]
  limits: Record<string, number>
  rules: string[]
}

export interface AgentInfo {
  code: string
  display_name: string
  knowledge_bases: string[]
  tools: string[]
  output_schema: string
  knowledge_base_scope?: string[]
  tool_scope?: string[]
  /** 该 Agent 实际生效的模型目标（按 Agent 覆盖 > 全局默认；后端不返回密钥） */
  model_profile?: {
    key: string
    model: string
    base_url?: string | null
    provider: string
    configured: boolean
    source: 'global' | 'override' | string
    temperature: number
    max_output_tokens: number
  }
}

export interface AgentCatalog {
  agents: AgentInfo[]
  analysis_branches: string[]
  tools: Array<{ name: string; description: string; arg_schema: Record<string, unknown> }>
}

export interface EvaluationMetrics {
  pass_rate?: number
  refusal_accuracy?: number
  conflict_detection_rate?: number
  injection_handling_rate?: number
  human_takeover_rate?: number
  check_pass_rate?: number
  per_category?: Record<string, number>
  category_counts?: Record<string, number>
  case_count?: number
  avg_latency_ms?: number | null
  total_tokens?: number
  [key: string]: unknown
}

export interface EvaluationRunOut {
  id: string
  name: string
  dataset_version: string
  workflow_version?: string | null
  prompt_version?: string | null
  embedding_version?: string | null
  case_count: number
  status: string
  metrics: EvaluationMetrics
  details: Array<{
    case_id: string
    category: string
    question: string
    passed: boolean
    checks: Record<string, boolean>
    diagnostics?: Record<string, boolean>
    task_id?: string
    task_status?: string | null
    error_code?: string | null
    conflict_count?: number
    evidence_count?: number
    latency_ms?: number
  }>
  avg_latency_ms?: number | null
  started_at?: string | null
  finished_at?: string | null
}

// ===========================================================================
// 电商售后工单（需求书 §6 / §7）
// ===========================================================================

/** 工单主体。`allowed_transitions` 由后端状态机给出，前端不自己猜能点哪些按钮 */
export interface TicketOut {
  id: string
  tenant_id: string
  external_id?: string | null
  customer_id?: string | null
  order_id?: string | null
  shop_id?: string | null
  platform?: string | null
  subject: string
  content: string
  channel: string
  status: string
  issue_type?: string | null
  priority: string
  sentiment?: string | null
  risk_tier?: string | null
  assignee_id?: string | null
  last_analysis_task_id?: string | null
  analysis_count: number
  missing_fields: string[]
  attributes: Record<string, unknown>
  data_origin: 'business' | 'demo' | 'test'
  escalation_reason?: string | null
  created_at: string
  updated_at: string
  closed_at?: string | null
  allowed_transitions: string[]
  sla_due_at?: string | null
  sla_remaining_seconds?: number | null
  sla_status?: 'on_track' | 'at_risk' | 'overdue' | 'completed' | string
  next_action?: string
  handler_label?: string
  quality_status?: 'unreviewed' | 'pass' | 'needs_followup' | 'block' | string
  quality_issue_codes?: string[]
  quality_reviewed_at?: string | null
  quality_reviewer_id?: string | null
}

export interface TicketAssignee {
  id: string
  username: string
  full_name?: string | null
  department?: string | null
  /** 有 ticket:approve 的人（主管/管理员）——L3 工单应该转给他 */
  can_approve: boolean
}

export interface TicketAssignmentCandidate {
  user_id: string
  username: string
  full_name?: string | null
  department?: string | null
  can_approve: boolean
  active_ticket_count: number
  high_priority_count: number
  waiting_review_count: number
  score: number
  reasons: string[]
}

export interface TicketAssignmentRecommendations {
  ticket_id: string
  current_assignee_id?: string | null
  issue_type_context?: string | null
  rule_version: string
  items: TicketAssignmentCandidate[]
}

export interface TicketMessageOut {
  id: string
  sender_type: string
  actor_id?: string | null
  content: string
  visible_to_customer: boolean
  meta?: {
    message_type?: string
    mention_user_ids?: string[]
    quality_result?: 'pass' | 'needs_followup' | 'block' | string
    quality_issue_codes?: string[]
    [key: string]: unknown
  }
  created_at: string
}

export interface ReplyTemplateOut {
  id: string
  tenant_id: string
  title: string
  content: string
  issue_type?: string | null
  active: boolean
  created_by?: string | null
  updated_by?: string | null
  created_at: string
  updated_at: string
}

export interface TicketMentionOut {
  id: string
  ticket_id: string
  ticket_external_id?: string | null
  subject: string
  content: string
  actor_id?: string | null
  created_at: string
  read: boolean
  mention_user_ids: string[]
}

export interface TicketFactOut {
  id: string
  source: string
  fact_type: string
  payload: Record<string, unknown>
  observed_at?: string | null
  request_id?: string | null
  is_mock: boolean
  adapter?: string | null
  stale: boolean
}

/** 详情页左栏"用户与订单"：键名逐单变化，所以后端给 {key,value}[]，前端只负责画 */
export interface TicketFactRow {
  key: string
  label: string
  value: unknown
  source: string
  observed_at?: string | null
  is_mock: boolean
  stale: boolean
}

export interface TicketTimelineItem {
  at: string
  kind: string
  title: string
  detail?: string | null
}

export interface TicketAgentStep {
  agent_code: string
  short: string
  name: string
  status: string
  model?: string | null
  latency_ms?: number | null
  total_tokens?: number | null
  attempt?: number
  error?: string | null
}

export interface TicketBusinessStep {
  agent_code: string
  role: string
  responsibility: string
  task: string
  why: string
  phase: string
  parallel_group?: string | null
  status: string
  conclusion: string
}

export interface TicketPolicyRef {
  title?: string
  version?: string | null
  clause?: string | null
  snippet?: string
  effective_from?: string | null
  evidence_ref?: string | null
}

export interface TicketRecommendationOut {
  id: string
  ticket_id: string
  analysis_task_id?: string | null
  action_type: string
  amount?: string | number | null
  /** 金额来源（"支付事实（实付金额）"/"政策条款（超时补偿标准）"）：审批人据此核对金额是否可信 */
  amount_source?: string | null
  currency: string
  rationale?: string | null
  evidence_ids: string[]
  policy_refs: TicketPolicyRef[]
  missing_information: string[]
  risk_tier: string
  risk_reason?: string | null
  approval_requirement: string
  confidence?: number | null
  status: string
  customer_reply?: string | null
  internal_note?: string | null
  warnings: string[]
  decided_by?: string | null
  decided_at?: string | null
  decision_reason?: string | null
  original_reply?: string | null
  created_at: string
  approval_label?: string | null
  auto_draft_allowed: boolean
  /** 未审批的写操作（退款/赔付）必须禁用按钮 —— 需求书 FR-05 */
  write_action_blocked: boolean
  answerability_code?: 'ready_to_reply' | 'staff_confirm' | 'need_information' | 'human_review' | string
  answerability_label?: string
  answerability_reason?: string | null
  reply_commitment_blocked?: boolean
}

export interface TicketCost {
  currency: string
  input_tokens: number
  output_tokens: number
  cache_hit_tokens: number
  amount: number
  calls: number
  total_tokens: number
  per_model: Record<
    string,
    { input_tokens: number; output_tokens: number; amount: number; calls: number }
  >
  unpriced_models: string[]
  ticket_id: string
  analysis_count: number
  amount_per_analysis: number
  per_analysis: Array<{
    task_id: string
    amount: number
    total_tokens: number
    calls: number
    cache_hit_tokens: number
  }>
  batch_estimate?: {
    per_ticket_amount: number
    daily_volume: number
    daily_amount: number
    monthly_amount: number
    yearly_amount: number
  }
}

export interface TicketDetailOut {
  ticket: TicketOut
  messages: TicketMessageOut[]
  facts: TicketFactOut[]
  recommendation?: TicketRecommendationOut | null
  fact_rows: TicketFactRow[]
  timeline: TicketTimelineItem[]
  analysis?: {
    mode?: 'fast_path' | 'multi_agent' | null
    mode_label?: string | null
    route_reason?: string
    agent_count?: number
    business_steps?: TicketBusinessStep[]
    applied_skills?: string[]
    analysis_task_id?: string | null
    analysis_count: number
    steps: TicketAgentStep[]
    confidence?: number | null
    risk_tier?: string | null
    reflection?: {
      enabled: boolean
      label: string
      verdict?: 'pass' | 'revise' | 'retrieve_more' | 'human_review' | null
      issue_count: number
      revised: boolean
      rounds: number
      issues: Array<{
        code: string
        severity: 'low' | 'medium' | 'high' | 'critical'
        category: string
        message: string
        auto_fixable: boolean
      }>
      stop_reason?: string | null
    }
  } | null
  handoff_summary?: {
    status_label: string
    customer_issue: string
    known_facts: string[]
    missing_information: string[]
    suggested_action: string
    risk_label?: string | null
    reason?: string | null
    next_step: string
  } | null
  cost?: TicketCost | null
}

export interface TicketStatsOut {
  today_new: number
  pending: number
  pending_high_priority: number
  waiting_review: number
  avg_first_response_minutes?: number | null
  ai_acceptance_rate?: number | null
  ai_decided_count: number
  waiting_customer?: number
  waiting_reply?: number
  at_risk?: number
  resolved_today?: number
  excluded_test_records?: number
  issue_type_options: string[]
  status_options: string[]
}

export interface TicketListResponse {
  items: TicketOut[]
  meta: { total: number; page: number; page_size: number }
}

export interface AfterSalesPolicyOut {
  id: string
  title: string
  version: string
  scope: Record<string, unknown>
  effective_from?: string | null
  effective_to?: string | null
  status: string
  /** active / expired / future / archived —— 界面据此上色 */
  state: string
  usable: boolean
  reason: string
  document_id?: string | null
  kb_code?: string | null
  note?: string | null
}

export interface PolicySearchTestOut {
  query: string
  at: string
  shop_id?: string | null
  platform?: string | null
  member_level?: string | null
  hits: Array<{
    chunk_id: string
    document_id: string
    source_name: string
    section_title?: string | null
    score: number
    snippet: string
    policy_version?: string | null
    /** 命中切片的生效期（后端 PolicyService.search_test 逐条给出，界面用来佐证"引用的是现行版本"） */
    effective_from?: string | null
    effective_to?: string | null
    usable: boolean
    reason: string
  }>
  excluded_policies: AfterSalesPolicyOut[]
  excluded_document_ids: string[]
  /** 判定时点（与 at 同值，后端另给的显式字段名） */
  checked_at?: string
}

/** 政策状态取值（GET /policies/status-options；与后端 PolicyStatus 枚举一致） */
export interface PolicyStatusOptionsOut {
  statuses: string[]
}

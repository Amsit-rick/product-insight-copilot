/** 按后端路由分组的接口封装（路径与 app/api/routes_*.py 一一对应）。 */

import { configureCsrf, http } from './client'
import type {
  AccessPolicyCreate,
  AccessPolicyOut,
  AuthorizationSimulation,
  AfterSalesPolicyOut,
  AgentCatalog,
  ApprovalOut,
  CanaryExperiment,
  CostSummary,
  DeliveryReadinessSnapshot,
  PolicySearchTestOut,
  PolicyStatusOptionsOut,
  QaAnswer,
  QaHistoryPage,
  QaConversationMessagesPage,
  QaConversationPage,
  DocumentOut,
  EvaluationRunOut,
  KnowledgeBaseOut,
  ListResponse,
  MeOut,
  MetricDefinitionOut,
  MetricImportResponse,
  MetricQueryResponse,
  OperationalAlert,
  OperationalAlertDelivery,
  OperationalAlertDeliveryList,
  OperationalAlertList,
  ProactiveDiagnosisResult,
  ProactiveScanResult,
  ProactiveTriggerStatus,
  PolicySimulation,
  QueueItemsResponse,
  QueueStats,
  ReportOut,
  ReflectionReleaseReport,
  SessionResponse,
  TaskEventOut,
  TaskExecutionUnit,
  TaskLiveSnapshot,
  TaskOut,
  TaskTrace,
  TicketCost,
  TicketAssignee,
  TicketAssignmentRecommendations,
  TicketDetailOut,
  ReplyTemplateOut,
  TicketMentionOut,
  TicketListResponse,
  TicketMessageOut,
  TicketOut,
  TicketRecommendationOut,
  TicketStatsOut,
  UserOut,
  WorkflowInfo,
} from './types'

const API = '/api/v1'

export interface TaskCreatePayload {
  query: string
  title?: string
  product?: string
  platform?: string
  app_version?: string
  channel?: string
  device?: string
  date_from?: string
  date_to?: string
  department?: string
  knowledge_bases?: string[]
  metrics?: string[]
  priority?: string
  force_human_review?: boolean
  idempotency_key?: string
}

export const authApi = {
  createSession: async (username: string, password: string) => {
    const session = await http.post<SessionResponse>(`${API}/auth/session`, { username, password })
    configureCsrf(session.csrf_cookie_name, session.csrf_header_name)
    return session
  },
  me: async () => {
    const me = await http.get<MeOut>(`${API}/auth/me`)
    // 页面刷新后 JS 内存会重置；从认证态接口恢复当前 CSRF 配置再允许后续写请求。
    configureCsrf(me.csrf_cookie_name, me.csrf_header_name)
    return me
  },
  logout: () => http.post<void>(`${API}/auth/logout`),
}

export const taskApi = {
  create: (payload: TaskCreatePayload) => http.post<TaskOut>(`${API}/tasks`, payload),
  list: (query: {
    status?: string
    mine?: boolean
    keyword?: string
    page?: number
    page_size?: number
  }) => http.get<ListResponse<TaskOut>>(`${API}/tasks`, query),
  summary: () =>
    http.get<{
      counts: Record<string, number>
      pending_review: number
      failed: number
      running: number
    }>(`${API}/tasks/summary`),
  get: (taskId: string) => http.get<TaskOut>(`${API}/tasks/${taskId}`),
  tree: (taskId: string) => http.get<TaskExecutionUnit>(`${API}/tasks/${taskId}/tree`),
  snapshot: (taskId: string) => http.get<TaskLiveSnapshot>(`${API}/tasks/${taskId}/snapshot`),
  streamUrl: (taskId: string, maxSeconds = 60) =>
    `${API}/tasks/${taskId}/stream?max_seconds=${encodeURIComponent(maxSeconds)}`,
  events: (taskId: string, after?: string) =>
    http.get<ListResponse<TaskEventOut>>(`${API}/tasks/${taskId}/events`, { after, limit: 500 }),
  trace: (taskId: string) => http.get<TaskTrace>(`${API}/tasks/${taskId}/trace`),
  report: (taskId: string) => http.get<ReportOut>(`${API}/tasks/${taskId}/report`),
  cancel: (taskId: string, expectedRevision?: number) =>
    http.post<TaskOut>(`${API}/tasks/${taskId}/cancel`, { expected_revision: expectedRevision }),
  retry: (taskId: string, reason?: string, expectedRevision?: number) =>
    http.post<TaskOut>(`${API}/tasks/${taskId}/retry`, {
      reason,
      expected_revision: expectedRevision,
    }),
  approve: (
    taskId: string,
    action: 'approve' | 'reject' | 'modify',
    comment?: string,
    modifications?: Record<string, unknown>,
    expectedRevision?: number,
  ) =>
    http.post<TaskOut>(`${API}/tasks/${taskId}/approve`, {
      action,
      comment,
      modifications,
      expected_revision: expectedRevision,
    }),
}

export const approvalApi = {
  list: (status = 'pending') =>
    http.get<{ items: ApprovalOut[]; total: number }>(`${API}/approvals`, { status }),
  get: (approvalId: string) => http.get<ApprovalOut>(`${API}/approvals/${approvalId}`),
  decide: (
    approvalId: string,
    action: 'approve' | 'reject' | 'modify',
    comment?: string,
    modifications?: Record<string, unknown>,
    expectedRevision?: number,
  ) =>
    http.post<TaskOut>(`${API}/approvals/${approvalId}/decision`, {
      action,
      comment,
      modifications,
      expected_revision: expectedRevision,
    }),
}

export const reportApi = {
  get: (reportId: string) => http.get<ReportOut>(`${API}/reports/${reportId}`),
  byTask: (taskId: string) =>
    http.get<{ items: Array<Partial<ReportOut>>; total: number }>(`${API}/reports`, { task_id: taskId }),
  exportMarkdown: (reportId: string) =>
    http.text(`${API}/reports/${reportId}/export`, { format: 'md' }),
  exportJson: (reportId: string) => http.text(`${API}/reports/${reportId}/export`, { format: 'json' }),
}

export const knowledgeApi = {
  list: () => http.get<KnowledgeBaseOut[]>(`${API}/knowledge-bases`),
  documents: (query: { kb_code?: string; status?: string; page?: number; page_size?: number }) =>
    http.get<ListResponse<DocumentOut>>(`${API}/documents`, query),
  upload: (form: FormData) => http.post<{ document: DocumentOut; chunks: number; warnings: string[]; index_status: string }>(`${API}/documents`, form),
  remove: (documentId: string) =>
    http.del<{ document_id: string; deleted_chunks: number }>(`${API}/documents/${documentId}`),
  reindex: (kbCode: string, force = false) =>
    http.post<{
      kb_code: string
      documents: number
      chunks: number
      failed: Array<{ document_id: string; error: string }>
      index_version?: string
      embedding_version?: string
    }>(`${API}/knowledge-bases/${kbCode}/reindex`, { force }),
}

export const metricApi = {
  definitions: () => http.get<MetricDefinitionOut[]>(`${API}/metrics/definitions`),
  coverage: (metricName: string) =>
    http.get<{
      metric_name: string
      min_date?: string | null
      max_date?: string | null
      rows: number
      versions: Array<{ app_version: string; rows: number; min_date?: string; max_date?: string }>
    }>(`${API}/metrics/coverage`, { metric_name: metricName }),
  trend: (query: Record<string, unknown>) => http.get<MetricQueryResponse>(`${API}/metrics/trend`, query),
  compare: (query: Record<string, unknown>) => http.get<MetricQueryResponse>(`${API}/metrics/compare`, query),
  breakdown: (query: Record<string, unknown>) =>
    http.get<MetricQueryResponse>(`${API}/metrics/breakdown`, query),
  anomaly: (query: Record<string, unknown>) => http.get<MetricQueryResponse>(`${API}/metrics/anomaly`, query),
  import: (form: FormData) => http.post<MetricImportResponse>(`${API}/metrics/import`, form),
}

export const systemApi = {
  workflow: () => http.get<WorkflowInfo>(`${API}/workflow`),
  agents: () => http.get<AgentCatalog>(`${API}/agents`),
  health: () =>
    http.get<{
      status: string
      app: string
      env: string
      database: string
      llm: {
        provider: string
        enabled: boolean
        model: string
        routing?: Array<{ key: string; model: string; source: string; configured: boolean }>
        runtime_metrics?: {
          chat_calls: number
          chat_json_calls: number
          json_parse_failures: number
          json_retries: number
          fallbacks: number
        }
        canary?: Array<{ key: string; model: string; percent: number; enabled: boolean }>
        canary_experiment?: { configured: boolean; min_sample: number }
      }
      embedding: { provider: string; enabled: boolean; model: string; dim: number }
      vector_backend: string
      workflow_version: string
      prompt_version: string
      limits?: Record<string, unknown>
      cache?: {
        backend: string
        hits: number
        misses: number
        hit_rate: number
        total_lookups?: number
        recent?: { window: number; hits: number; hit_rate: number }
        namespaces?: Record<
          string,
          { hits: number; misses: number; total_lookups: number; hit_rate: number }
        >
        healthy?: boolean
        degraded_reason?: string
      }
      cost?: { currency: string; priced_models: string[] }
      alerting?: {
        webhook: {
          enabled: boolean
          configured: boolean
          signed: boolean
          max_attempts: number
        }
        delivery_queue: {
          pending?: number
          claimed?: number
          sent?: number
          dead?: number
          error?: string
        }
      }
      // 执行与恢复：决定"能不能多副本跑"
      execution?: {
        backend: string
        configured_backend: string
        worker_embedded: boolean
        worker_concurrency: number
        checkpointer: { backend: string; shared: boolean; note?: string; path?: string }
        queue: { pending: number; claimed: number; done: number; dead: number; oldest_pending_at?: string | null }
      }
    }>('/health'),
  /** 成本汇总（近 N 天）：看板与成本控制用 */
  costs: (days = 7) => http.get<CostSummary>(`${API}/costs/summary`, { days }),
  /**
   * 灰度效果对比：按 model_source 分组统计灰度侧与对照侧。
   * 样本不足时接口本身只会返回"样本不足"，不会给谁更好的结论。
   */
  canaryExperiment: (days = 14, agentCode?: string) =>
    http.get<CanaryExperiment>(`${API}/experiments/canary`, { days, agent_code: agentCode }),
  reflectionRelease: (days = 14) =>
    http.get<ReflectionReleaseReport>(`${API}/experiments/reflection-release`, { days }),
  deliveryReadiness: () =>
    http.get<DeliveryReadinessSnapshot>(`${API}/delivery-readiness`),
  reflectionReleaseDecision: (payload: {
    action: 'evaluate' | 'approve_promotion' | 'pause' | 'rollback' | 'resume'
    percent?: number
    reason?: string
  }) =>
    http.post<ReflectionReleaseReport>(`${API}/experiments/reflection-release/decision`, payload),
  auditLogs: (query: { action?: string; limit?: number }) =>
    http.get<{
      items: Array<{
        id: string
        action: string
        actor_id?: string
        resource_type?: string
        resource_id?: string
        outcome: string
        detail?: Record<string, unknown>
        created_at: string
      }>
      total: number
    }>(`${API}/audit-logs`, query),
}

export const operationalAlertApi = {
  list: (query: { status?: string; severity?: string; limit?: number } = {}) =>
    http.get<OperationalAlertList>(`${API}/operational-alerts`, query),
  acknowledge: (alertId: string) =>
    http.post<OperationalAlert>(`${API}/operational-alerts/${alertId}/acknowledge`),
  resolve: (alertId: string) =>
    http.post<OperationalAlert>(`${API}/operational-alerts/${alertId}/resolve`),
  proactiveStatus: () =>
    http.get<ProactiveTriggerStatus>(`${API}/operational-alerts/proactive-trigger`),
  scanProactive: () =>
    http.post<ProactiveScanResult>(`${API}/operational-alerts/proactive-trigger/scan`),
  createDiagnosis: (alertId: string) =>
    http.post<ProactiveDiagnosisResult>(
      `${API}/operational-alerts/${alertId}/create-diagnosis`,
    ),
  deliveries: (query: { status?: string; limit?: number } = {}) =>
    http.get<OperationalAlertDeliveryList>(`${API}/operational-alerts/deliveries`, query),
  replayDelivery: (deliveryId: string) =>
    http.post<OperationalAlertDelivery>(
      `${API}/operational-alerts/deliveries/${deliveryId}/replay`,
    ),
}

/** 知识库问答（检索即答）：轻量入口，带引用；需要严谨结论时转完整诊断 */
export const qaApi = {
  ask: (payload: {
    query: string
    conversation_id?: string
    knowledge_bases?: string[]
    app_version?: string
    date_from?: string
    date_to?: string
    top_k?: number
    use_model?: boolean
  }) => http.post<QaAnswer>(`${API}/qa/ask`, payload),
  history: (query: { page?: number; page_size?: number } = {}) =>
    http.get<QaHistoryPage>(`${API}/qa/history`, query),
  conversations: (query: { page?: number; page_size?: number } = {}) =>
    http.get<QaConversationPage>(`${API}/qa/conversations`, query),
  messages: (conversationId: string, query: { page?: number; page_size?: number } = {}) =>
    http.get<QaConversationMessagesPage>(
      `${API}/qa/conversations/${conversationId}/messages`,
      query,
    ),
}

/** 访问策略（仅管理员）：统一管理指标、任务回放、工单、知识库与工具授权。 */
export const policyApi = {
  list: () => http.get<AccessPolicyOut[]>(`${API}/access-policies`),
  create: (payload: AccessPolicyCreate) => http.post<AccessPolicyOut>(`${API}/access-policies`, payload),
  remove: (policyId: string) => http.del<void>(`${API}/access-policies/${policyId}`),
  /** 权限预演：给定账号/角色去问"这个指标能不能读、为什么"（与真实判定同一份实现） */
  simulate: (payload: {
    username?: string
    roles?: string[]
    clearance?: number
    metric: string
    resource_type?: string
    requested?: Record<string, string>
  }) => http.post<PolicySimulation>(`${API}/access-policies/simulate`, payload),
  authorizeSimulate: (payload: {
    username?: string
    roles?: string[]
    clearance?: number
    department?: string
    action: string
    resource_type: string
    resource_id: string
    resource_tenant_id?: string
    context?: Record<string, unknown>
  }) => http.post<AuthorizationSimulation>(`${API}/access-policies/authorize-simulate`, payload),
}

/** 账号列表（仅管理员）：密级策略页的"预演对象"下拉用。
 *  注意路径是 /auth/users（认证路由带 /auth 前缀），不是 /users。 */
export const userApi = {
  list: () => http.get<UserOut[]>(`${API}/auth/users`),
}

/** 队列运维（仅管理员）：死信排查与重放 */
export const queueApi = {
  items: (query: { status?: string; limit?: number } = {}) =>
    http.get<QueueItemsResponse>(`${API}/queue/items`, query),
  replay: (itemId: string) =>
    http.post<{ item_id: string; replayed: boolean; stats: QueueStats }>(
      `${API}/queue/items/${itemId}/replay`,
      {},
    ),
  replayDead: () =>
    http.post<{ replayed: number; stats: QueueStats }>(`${API}/queue/replay-dead`, {}),
}

export const evaluationApi = {  cases: (query: { category?: string; limit?: number }) =>
    http.get<{
      items: Array<{
        id: string
        category: string
        question: string
        expected_kb?: string | null
        expected_intent?: string | null
        notes?: string | null
      }>
      total: number
    }>(`${API}/evaluations/cases`, query),
  runs: (limit = 20) => http.get<EvaluationRunOut[]>(`${API}/evaluations/runs`, { limit }),
  run: (payload: { dataset_version?: string; name?: string; limit?: number; categories?: string[] }) =>
    http.post<EvaluationRunOut>(`${API}/evaluations/run`, payload),
  /** 售后工单评测（需求书 §9）：分类 Macro-F1 / 审批门禁召回 / 政策命中 / P95 / 每单成本 */
  ticketsRun: (
    params: {
      limit?: number
      groups?: string[]
      name?: string
      /** 真实模型下必须显式确认（后端 409 real_model_confirmation_required 时带上它重试） */
      confirm_real_model?: boolean
    } = {},
  ) => http.post<EvaluationRunOut>(`${API}/evaluations/tickets/run`, undefined, params),
  ticketsCases: (limit = 100) =>
    http.get<{
      dataset_version: string
      total: number
      llm_provider: string
      real_model: boolean
      estimated_cost_per_case: number
      estimated_cost_total: number
      items: Array<Record<string, unknown>>
    }>(`${API}/evaluations/tickets/cases`, { limit }),
}

/**
 * 电商售后工单（需求书 §6）。
 *
 * 注意命名：`policyApi` 已被"指标密级策略"占用，所以售后政策用 `afterSalesPolicyApi`。
 */
export const ticketApi = {
  list: (query: {
    status?: string[]
    issue_type?: string[]
    priority?: string[]
    assignee_id?: string
    mine?: boolean
    scope?: 'all' | 'mine' | 'unassigned'
    attention?: 'at_risk' | 'waiting_reply' | 'waiting_customer' | 'waiting_review' | 'resolved_today'
    keyword?: string
    include_test?: boolean
    page?: number
    page_size?: number
  }) => http.get<TicketListResponse>(`${API}/tickets`, query),
  qualityReviews: (query: {
    quality_status?: 'unreviewed' | 'needs_followup' | 'block' | 'pass'
    quality_issue_code?: string
    keyword?: string
    page?: number
    page_size?: number
  }) => http.get<TicketListResponse>(`${API}/tickets/quality-reviews`, query),
  qualityReviewsExport: (query: {
    quality_status?: 'unreviewed' | 'needs_followup' | 'block' | 'pass'
    quality_issue_code?: string
    keyword?: string
  }) => http.text(`${API}/tickets/quality-reviews/export`, query),
  stats: (query?: { scope?: 'all' | 'mine' | 'unassigned' }) =>
    http.get<TicketStatsOut>(`${API}/tickets/stats`, query),
  /** 转派候选人（客服自己就能取，不需要管理员权限） */
  assignees: () =>
    http.get<{ items: TicketAssignee[]; total: number }>(`${API}/tickets/assignees`),
  assignmentRecommendations: (ticketId: string) =>
    http.get<TicketAssignmentRecommendations>(
      `${API}/tickets/${ticketId}/assignment-recommendations`,
    ),
  stateMachine: () =>
    http.get<{ transitions: Record<string, { next: string[]; terminal: boolean }>; terminal: string[] }>(
      `${API}/tickets/state-machine`,
    ),
  detail: (ticketId: string) => http.get<TicketDetailOut>(`${API}/tickets/${ticketId}`),
  create: (payload: {
    subject: string
    content: string
    external_id?: string
    customer_id?: string
    order_id?: string
    priority?: string
    issue_type?: string
    channel?: string
    attributes?: Record<string, unknown>
    data_origin?: 'business' | 'demo' | 'test'
    idempotency_key?: string
  }) => http.post<TicketOut>(`${API}/tickets`, payload),
  analyze: (ticketId: string) =>
    http.post<{ ticket_id: string; run_id: string; status: string }>(
      `${API}/tickets/${ticketId}/analyze`,
    ),
  updateOrder: (ticketId: string, orderId: string) =>
    http.patch<TicketOut>(`${API}/tickets/${ticketId}/order`, { order_id: orderId }),
  assign: (ticketId: string, payload: { assignee_id?: string | null; reason?: string }) =>
    http.post<TicketOut>(`${API}/tickets/${ticketId}/assign`, payload),
  transition: (ticketId: string, payload: { target: string; reason?: string }) =>
    http.post<TicketOut>(`${API}/tickets/${ticketId}/transition`, payload),
  reply: (ticketId: string, payload: {
    content: string
    visible_to_customer?: boolean
    message_type?: 'reply' | 'internal_note'
    mention_user_ids?: string[]
    idempotency_key?: string
    quality_review?: boolean
    quality_result?: 'pass' | 'needs_followup' | 'block'
    quality_issue_codes?: string[]
  }) =>
    http.post<TicketMessageOut>(`${API}/tickets/${ticketId}/messages`, payload),
  cost: (ticketId: string, dailyVolume?: number) =>
    http.get<TicketCost>(`${API}/tickets/${ticketId}/cost`, { daily_volume: dailyVolume }),
  recommend: (ticketId: string) =>
    http.get<TicketRecommendationOut | null>(`${API}/tickets/${ticketId}/recommendation`),
  decide: (
    recommendationId: string,
    payload: { decision: 'approve' | 'reject'; reason?: string; modified_reply?: string; idempotency_key?: string },
  ) => http.post<TicketRecommendationOut>(`${API}/recommendations/${recommendationId}/decision`, payload),
  send: (recommendationId: string, payload?: { content?: string; idempotency_key?: string }) =>
    http.post<TicketRecommendationOut>(`${API}/recommendations/${recommendationId}/send`, payload),
  templates: (issueType?: string) =>
    http.get<ReplyTemplateOut[]>(`${API}/ticket-templates`, { issue_type: issueType }),
  createTemplate: (payload: { title: string; content: string; issue_type?: string | null }) =>
    http.post<ReplyTemplateOut>(`${API}/ticket-templates`, payload),
  updateTemplate: (templateId: string, payload: { title?: string; content?: string; issue_type?: string | null; active?: boolean }) =>
    http.patch<ReplyTemplateOut>(`${API}/ticket-templates/${templateId}`, payload),
  deleteTemplate: (templateId: string) => http.del<void>(`${API}/ticket-templates/${templateId}`),
  mentions: (unreadOnly = false) =>
    http.get<TicketMentionOut[]>(`${API}/ticket-mentions`, { unread_only: unreadOnly, limit: 50 }),
  markMentionRead: (messageId: string) =>
    http.post<TicketMentionOut>(`${API}/ticket-mentions/${messageId}/read`),
}

/** 售后政策库：版本、生效期、适用范围与检索测试（与"指标密级策略"是两回事） */
export const afterSalesPolicyApi = {
  /** 政策清单：shop_id/platform/member_level/at 是"适用范围过滤"的入口，可用性判定由后端给出 */
  list: (query?: { shop_id?: string; platform?: string; member_level?: string; at?: string }) =>
    http.get<{ items: AfterSalesPolicyOut[]; count: number; usable_count: number; at: string }>(
      `${API}/policies`,
      query,
    ),
  /** 政策状态取值（draft/active/expired/archived），界面用于渲染状态标签 */
  statusOptions: () => http.get<PolicyStatusOptionsOut>(`${API}/policies/status-options`),
  create: (payload: {
    title: string
    version: string
    content: string
    effective_from?: string
    effective_to?: string
    scope?: Record<string, unknown>
    note?: string
  }) => http.post<Record<string, unknown>>(`${API}/policies/documents`, payload),
  setStatus: (policyId: string, status: 'active' | 'archived', reason: string) =>
    http.post<Record<string, unknown>>(`${API}/policies/documents/${policyId}/status`, { status, reason }),
  searchTest: (payload: {
    query: string
    top_k?: number
    at?: string
    shop_id?: string
    platform?: string
    member_level?: string
  }) =>
    http.post<PolicySearchTestOut>(`${API}/policies/search-test`, payload),
}

<script setup lang="ts">
/**
 * 售后工单工作台（需求书 §7 原型对齐）。
 *
 * 页面结构（与原型一致）：
 *   标题行 + 4 张指标卡 + 筛选条 + 7 列表格 + 分页
 *   → 点行打开右侧详情抽屉：下一步 + 客户反馈 / 已核实信息 / 建议处理 / 处理记录
 *   → 抽屉内动作（转派/升级/审批/发送）都走确认弹窗，理由必填
 *
 * 三条设计取舍写在代码里：
 * 1. **能点哪些按钮由后端状态机给**（`allowed_transitions`），前端不硬编码状态判断 ——
 *    否则前后端各写一套，用户点了按钮才报 409。
 * 2. **进度用 SSE**（`GET /runs/{id}/events`），断流时自动降级为轮询：
 *    EventSource 无法统一处理本项目的会话恢复与错误回退，所以用 fetch + ReadableStream 手工解析帧。
 * 3. **未审批的写操作按钮禁用**（`write_action_blocked`）：需求书 FR-05 要求
 *    "未审批不能调用退款/赔付类写操作"，这条必须在界面上也挡住，不能只靠后端 409。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { afterSalesPolicyApi, ticketApi } from '@/api/endpoints'
import { useSessionStore } from '@/stores/session'
import type {
  AfterSalesPolicyOut,
  ReplyTemplateOut,
  TicketAssignmentCandidate,
  TicketAssignee,
  TicketDetailOut,
  TicketMentionOut,
  TicketOut,
  TicketStatsOut,
} from '@/api/types'

const session = useSessionStore()
type TicketScope = 'all' | 'mine' | 'unassigned'
type AttentionQueue = 'at_risk' | 'waiting_reply' | 'waiting_customer' | 'waiting_review' | 'resolved_today'

const STATUS_TEXT: Record<string, string> = {
  new: '待处理',
  enriching: '补事实',
  analyzing: '分析中',
  waiting_review: '待审批',
  ready_to_reply: '待回复',
  resolved: '已解决',
  need_customer_info: '等客户补充',
  escalated: '已升级',
  failed: '失败',
  cancelled: '已取消',
}

const ISSUE_TEXT: Record<string, string> = {
  refund: '退款申请',
  return: '退货',
  exchange: '换货',
  logistics_stalled: '物流停滞',
  missing_item: '少件',
  damaged: '商品破损',
  wrong_item: '错发',
  not_shipped: '未发货',
  promotion_not_applied: '优惠问题',
  description_mismatch: '描述不符',
  other: '其他',
}

const PRIORITY_TEXT: Record<string, string> = {
  low: '低',
  medium: '中',
  high: '高',
  urgent: '紧急',
}

const CHANNEL_TEXT: Record<string, string> = {
  manual: '后台录入',
  api: '接口导入',
  wecom: '企业微信',
  feishu: '飞书',
  customer_service: '客服系统',
}

const ACTION_TEXT: Record<string, string> = {
  reply_only: '仅解释说明',
  ask_customer: '向客户索取信息',
  refund: '发起退款',
  return_goods: '退货处理',
  exchange: '换货处理',
  reship: '补发商品',
  compensate: '赔付',
  coupon: '发放优惠券',
  contact_logistics: '联系承运商',
  escalate: '升级处理',
  reject: '驳回诉求',
}

const RISK_TIER_TEXT: Record<string, string> = {
  L1: '可直接准备回复',
  L2: '客服确认后处理',
  L3: '需要主管审批',
  L4: '需要风控介入',
}

const BUSINESS_SKILL_TEXT: Record<string, string> = {
  promotion_explanation: '优惠条件核对',
  refund_review: '退款依据复核',
  logistics_exception: '物流异常核对',
  fulfillment_exception: '发货数量核对',
  damage_evidence: '商品凭证核验',
  quality_guard: '证据与审批检查',
}

const stats = ref<TicketStatsOut | null>(null)
const tickets = ref<TicketOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const loading = ref(false)
const keyword = ref('')
const statusFilter = ref<string[]>([])
const issueFilter = ref<string[]>([])
const priorityFilter = ref<string[]>([])
const defaultTicketScope = (): TicketScope => session.can('ticket:approve') ? 'all' : 'mine'
const ticketScope = ref<TicketScope>(defaultTicketScope())
const attentionQueue = ref<AttentionQueue | null>(null)
const includeTestRecords = ref(false)

const detail = ref<TicketDetailOut | null>(null)
const drawerOpen = ref(false)
const detailLoading = ref(false)
const replyDraft = ref('')
const noteDraft = ref('')
const noteMentionIds = ref<string[]>([])
const messageSending = ref(false)
const progress = ref<Array<{ node: string; message: string; at?: string | null }>>([])
const streaming = ref(false)
let streamAbort: AbortController | null = null

const createVisible = ref(false)
const createForm = ref({
  subject: '',
  content: '',
  order_id: '',
  customer_id: '',
  priority: 'medium',
  // 风控标记 / 敏感客户：L4 与 L3 的判据，界面上要能造出来（否则 L4 只能靠脚本验）
  risk_flags: [] as string[],
  sensitive_customer: false,
})
const actionVisible = ref(false)
const missingOrderVisible = ref(false)
const orderIdDraft = ref('')
const orderSaving = ref(false)
const actionTitle = ref('')
const actionHint = ref('')
const actionReason = ref('')
const actionKind = ref<'assign' | 'escalate' | 'approve' | 'reject' | 'send' | 'create' | 'analyze'>(
  'assign',
)
/** 转派选中的负责人（不选就不让提交——以前固定提交 null，负责人列永远"未分配"） */
const actionAssignee = ref<string>('')
const assignees = ref<TicketAssignee[]>([])
const assignmentRecommendations = ref<TicketAssignmentCandidate[]>([])
const recommendationRuleVersion = ref('')
const assignmentRecommendationsLoading = ref(false)
const toast = ref<{ text: string; visible: boolean }>({ text: '', visible: false })
let toastTimer: number | undefined

const policies = ref<AfterSalesPolicyOut[]>([])
const quickReplies = ref<ReplyTemplateOut[]>([])
const mentions = ref<TicketMentionOut[]>([])
const mentionVisible = ref(false)
const templateVisible = ref(false)
const templateEditing = ref<ReplyTemplateOut | null>(null)
const templateForm = ref({ title: '', content: '', issue_type: '' })
const templateSaving = ref(false)

const DEFAULT_QUICK_REPLIES = [
  {
    label: '等待补充信息',
    content: '您好，为了尽快帮您处理，请补充页面提示的相关信息。收到后我们会继续为您核实。',
  },
  {
    label: '已提交主管审核',
    content: '您好，我们已将处理方案提交主管审核，审核结果会第一时间同步给您。',
  },
  {
    label: '物流核实中',
    content: '您好，我们正在联系承运商核实物流情况，确认后会及时向您同步处理结果。',
  },
]

function builtinReplyRows(): ReplyTemplateOut[] {
  return DEFAULT_QUICK_REPLIES.map((item, index) => ({
    id: `builtin-${index}`,
    tenant_id: '',
    title: item.label,
    content: item.content,
    issue_type: null,
    active: true,
    created_at: '',
    updated_at: '',
  }))
}

const canManageTemplates = computed(() => session.can('ticket:template_manage'))
const canViewTechnicalDetails = computed(
  () => session.can('admin:manage') || session.roles.some((role) => ['eng', 'engineer'].includes(role)),
)
const unreadMentionCount = computed(() => mentions.value.filter((item) => !item.read).length)
const workbenchTitle = computed(() => {
  if (ticketScope.value === 'mine') return '我的待办'
  if (ticketScope.value === 'unassigned') return '待分配工单'
  return '全部工单'
})
const ATTENTION_LABELS: Record<string, string> = {
  at_risk: '即将超时',
  waiting_reply: '等待回复',
  waiting_customer: '等待客户补充',
  waiting_review: '待主管审批',
  resolved_today: '今日已完成',
}
const attentionLabel = computed(() => ATTENTION_LABELS[attentionQueue.value || ''] ?? '')
const advancedFilterCount = computed(
  () => statusFilter.value.length + issueFilter.value.length + priorityFilter.value.length + Number(includeTestRecords.value),
)

const currentTicket = computed(() => detail.value?.ticket ?? null)
const recommendation = computed(() => detail.value?.recommendation ?? null)
const steps = computed(() => detail.value?.analysis?.steps ?? [])
const businessSteps = computed(() => detail.value?.analysis?.business_steps ?? [])
const assigneeNames = computed(
  () => new Map(assignees.value.map((item) => [item.id, item.full_name || item.username])),
)
const businessRoleSummary = computed(() => {
  const roles = [...new Set(businessSteps.value.map((step) => step.role).filter(Boolean))]
  return roles.join('、')
})

function showToast(text: string): void {
  toast.value = { text, visible: true }
  if (toastTimer) window.clearTimeout(toastTimer)
  // 原型是底部居中约 2.4 秒消失
  toastTimer = window.setTimeout(() => (toast.value.visible = false), 2400)
}

function statusText(status: string, missingFields: string[] = []): string {
  if (status === 'need_customer_info' && missingFields.some((item) => item.includes('事实来源暂不可用'))) {
    return '待人工核实'
  }
  return STATUS_TEXT[status] ?? status
}

function businessStepStatusText(status: string): string {
  return ({
    pending: '待开始',
    running: '处理中',
    succeeded: '已完成',
    failed: '需人工检查',
    skipped: '未执行',
    waiting_human: '等待人工确认',
  })[status] ?? '待确认'
}

function businessEventText(node: string): string {
  return ({
    triage_agent: '正在识别客户遇到的问题',
    order_fact_agent: '正在核对订单、支付和物流记录',
    policy_rag_agent: '正在核对适用的售后规则',
    resolution_agent: '正在整理处理建议',
    response_agent: '正在准备客户回复草稿',
    review_agent: '正在复核证据和处理风险',
    followup_agent: '正在安排后续跟进',
    decide_action: '正在按业务规则确认处理范围',
    finalize_recommendation: '正在汇总核验结果',
    load_ticket_context: '正在读取工单和已核验事实',
    runner: '系统正在处理，请稍候',
  })[node] ?? '正在核实相关信息'
}

function issueText(issue?: string | null): string {
  if (!issue) return '未分类'
  return ISSUE_TEXT[issue] ?? issue
}

function priorityText(priority: string): string {
  return PRIORITY_TEXT[priority] ?? priority
}

function channelText(channel?: string | null): string {
  if (!channel) return '未注明来源'
  return CHANNEL_TEXT[channel] ?? channel
}

function statusClass(status: string): string {
  if (status === 'resolved') return 'pill pill--done'
  if (status === 'waiting_review') return 'pill pill--warn'
  if (status === 'analyzing' || status === 'enriching' || status === 'ready_to_reply')
    return 'pill pill--doing'
  if (status === 'escalated' || status === 'failed') return 'pill pill--risk'
  return 'pill pill--todo'
}

function riskText(): string {
  const reco = recommendation.value
  if (!reco) return '待评估'
  const label = RISK_TIER_TEXT[reco.risk_tier] ?? '需要人工判断'
  return `${label}${reco.risk_reason ? ` · ${reco.risk_reason}` : ''}`
}

function relativeTime(value?: string | null): string {
  if (!value) return '—'
  const then = new Date(value.endsWith('Z') ? value : `${value}Z`).getTime()
  if (Number.isNaN(then)) return '—'
  const diff = Math.max(0, Date.now() - then)
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

function assigneeText(assigneeId?: string | null): string {
  if (!assigneeId) return '未分配'
  return assigneeNames.value.get(assigneeId) || `账号 ${assigneeId.slice(-6)}`
}

async function loadStats(): Promise<void> {
  try {
    stats.value = await ticketApi.stats({ scope: ticketScope.value })
  } catch (error) {
    showToast(`指标卡加载失败：${(error as Error).message}`)
  }
}

async function loadTickets(): Promise<void> {
  loading.value = true
  try {
    const response = await ticketApi.list({
      status: statusFilter.value.length ? statusFilter.value : undefined,
      issue_type: issueFilter.value.length ? issueFilter.value : undefined,
      priority: priorityFilter.value.length ? priorityFilter.value : undefined,
      scope: ticketScope.value,
      attention: attentionQueue.value || undefined,
      keyword: keyword.value || undefined,
      include_test: includeTestRecords.value || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    tickets.value = response.items
    total.value = response.meta.total
  } catch (error) {
    showToast(`工单列表加载失败：${(error as Error).message}`)
  } finally {
    loading.value = false
  }
}

async function loadPolicies(): Promise<void> {
  try {
    const response = await afterSalesPolicyApi.list()
    policies.value = response.items
  } catch {
    policies.value = []
  }
}

async function loadTemplates(issueType?: string | null): Promise<void> {
  try {
    const rows = await ticketApi.templates(issueType || undefined)
    const builtin = builtinReplyRows()
    quickReplies.value = [...rows, ...builtin.filter((fallback) => !rows.some((row) => row.title === fallback.title))]
  } catch {
    quickReplies.value = builtinReplyRows()
  }
}

async function loadMentions(): Promise<void> {
  try {
    mentions.value = await ticketApi.mentions()
  } catch {
    mentions.value = []
  }
}

function renderTemplate(content: string): string {
  const ticket = currentTicket.value
  const values: Record<string, string | number | null> = {
    订单号: ticket?.order_id || ticket?.external_id || null,
    问题类型: issueText(ticket?.issue_type),
    金额: recommendation.value?.amount ?? null,
  }
  const missing = new Set<string>()
  const rendered = content.replace(/\{\{([^{}]+)\}\}/g, (_match, rawName: string) => {
    const name = rawName.trim()
    const value = values[name]
    if (value === null || value === undefined || String(value).trim() === '') {
      missing.add(name)
      return `{{${name}}}`
    }
    return String(value)
  })
  if (missing.size) showToast(`模板仍缺少：${Array.from(missing).join('、')}，请补充后再发送`)
  return rendered
}

function openMentionPanel(): void {
  mentionVisible.value = true
  void loadMentions()
}

async function openMention(item: TicketMentionOut): Promise<void> {
  try {
    if (!item.read) await ticketApi.markMentionRead(item.id)
    await loadMentions()
    drawerOpen.value = true
    detailLoading.value = true
    detail.value = await ticketApi.detail(item.ticket_id)
    replyDraft.value = detail.value.recommendation?.customer_reply ?? ''
    mentionVisible.value = false
  } catch (error) {
    showToast(`打开待关注工单失败：${(error as Error).message}`)
  } finally {
    detailLoading.value = false
  }
}

function openTemplateManager(): void {
  templateEditing.value = null
  templateForm.value = { title: '', content: '', issue_type: '' }
  templateVisible.value = true
  void loadTemplates()
}

function editTemplate(item: ReplyTemplateOut): void {
  templateEditing.value = item
  templateForm.value = { title: item.title, content: item.content, issue_type: item.issue_type || '' }
}

async function saveTemplate(): Promise<void> {
  if (!templateForm.value.title.trim() || !templateForm.value.content.trim()) {
    showToast('请填写模板名称和内容')
    return
  }
  templateSaving.value = true
  try {
    const payload = {
      title: templateForm.value.title.trim(),
      content: templateForm.value.content.trim(),
      issue_type: templateForm.value.issue_type.trim() || null,
    }
    if (templateEditing.value) await ticketApi.updateTemplate(templateEditing.value.id, payload)
    else await ticketApi.createTemplate(payload)
    showToast(templateEditing.value ? '回复模板已更新' : '回复模板已创建')
    templateEditing.value = null
    templateForm.value = { title: '', content: '', issue_type: '' }
    await loadTemplates()
  } catch (error) {
    showToast(`保存模板失败：${(error as Error).message}`)
  } finally {
    templateSaving.value = false
  }
}

async function deactivateTemplate(item: ReplyTemplateOut): Promise<void> {
  try {
    await ticketApi.deleteTemplate(item.id)
    showToast('回复模板已停用')
    await loadTemplates()
  } catch (error) {
    showToast(`停用模板失败：${(error as Error).message}`)
  }
}

function resetFilters(): void {
  keyword.value = ''
  statusFilter.value = []
  issueFilter.value = []
  priorityFilter.value = []
  ticketScope.value = defaultTicketScope()
  attentionQueue.value = null
  includeTestRecords.value = false
  page.value = 1
  void Promise.all([loadTickets(), loadStats()])
}

function changeScope(): void {
  page.value = 1
  attentionQueue.value = null
  void Promise.all([loadTickets(), loadStats()])
}

function selectAttention(queue: AttentionQueue): void {
  attentionQueue.value = attentionQueue.value === queue ? null : queue
  statusFilter.value = []
  page.value = 1
  void loadTickets()
}

async function openTicket(ticket: TicketOut): Promise<void> {
  drawerOpen.value = true
  detailLoading.value = true
  // 不让上一张工单的内容在新详情请求期间继续留在抽屉里，避免客服对错单操作。
  detail.value = null
  progress.value = []
  try {
    detail.value = await ticketApi.detail(ticket.id)
    replyDraft.value = detail.value.recommendation?.customer_reply ?? ''
    noteDraft.value = ''
    noteMentionIds.value = []
    if (!assignees.value.length) void loadAssignees()
    void loadTemplates(detail.value.ticket.issue_type)
  } catch (error) {
    showToast(`工单详情加载失败：${(error as Error).message}`)
  } finally {
    detailLoading.value = false
  }
}

async function refreshDetail(): Promise<void> {
  if (!currentTicket.value) return
  try {
    detail.value = await ticketApi.detail(currentTicket.value.id)
    if (!replyDraft.value) replyDraft.value = detail.value.recommendation?.customer_reply ?? ''
  } catch {
    /* 轮询失败忽略，下一轮再试 */
  }
}

/**
 * 用 fetch 手工读 SSE：通过同源 HttpOnly Cookie 携带会话，断流时再用轮询兜底。
 * 断流/失败时降级为轮询 —— 进度可以慢，但不能因为流断了页面就卡住。
 */
async function streamProgress(runId: string): Promise<void> {
  streaming.value = true
  streamAbort = new AbortController()
  const refreshTimer = window.setInterval(() => void refreshDetail(), 1800)
  try {
    const response = await fetch(`/api/v1/runs/${runId}/events?max_seconds=60`, {
      credentials: 'same-origin',
      signal: streamAbort.signal,
    })
    if (!response.ok || !response.body) throw new Error(`SSE ${response.status}`)
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? ''
      for (const frame of frames) {
        const eventLine = frame.split('\n').find((line) => line.startsWith('event: '))
        const dataLine = frame.split('\n').find((line) => line.startsWith('data: '))
        if (!dataLine) continue
        const node = eventLine ? eventLine.slice(7) : 'event'
        if (node === 'done' || node === 'timeout') {
          streaming.value = false
          await refreshDetail()
          await loadTickets()
          await loadStats()
          return
        }
        if (node === 'open') continue
        const payload = JSON.parse(dataLine.slice(6)) as { message?: string; at?: string }
        const message = canViewTechnicalDetails.value
          ? payload.message ?? businessEventText(node)
          : businessEventText(node)
        progress.value = [
          ...progress.value,
          { node: canViewTechnicalDetails.value ? node : '', message, at: payload.at ?? null },
        ]
      }
    }
  } catch {
    /* 流断了不致命：下面用轮询兜底 */
  } finally {
    window.clearInterval(refreshTimer)
    streaming.value = false
    await refreshDetail()
  }
}

function askAnalyze(): void {
  if (!currentTicket.value?.order_id) {
    orderIdDraft.value = ''
    missingOrderVisible.value = true
    return
  }
  actionKind.value = 'analyze'
  actionTitle.value = '启动分析'
  actionHint.value = '将依次执行：问题分类 → 订单事实（并行政策检索）→ 方案 → 草稿与质检 → 跟进。'
  actionReason.value = ''
  actionVisible.value = true
}

async function startTicketAnalysis(ticket: TicketOut): Promise<void> {
  actionVisible.value = false
  streaming.value = true
  showToast('已启动分析，正在核实订单与处理规则')
  try {
    const response = await ticketApi.analyze(ticket.id)
    await Promise.all([loadTickets(), loadStats(), refreshDetail()])
    void streamProgress(response.run_id)
  } catch (error) {
    streaming.value = false
    await Promise.allSettled([loadTickets(), loadStats(), refreshDetail()])
    throw error
  }
}

async function saveOrderAndAnalyze(): Promise<void> {
  const ticket = currentTicket.value
  const orderId = orderIdDraft.value.trim()
  if (!ticket || !orderId) {
    showToast('请填写订单号')
    return
  }
  orderSaving.value = true
  try {
    const updated = await ticketApi.updateOrder(ticket.id, orderId)
    missingOrderVisible.value = false
    showToast('订单号已补录，开始查询订单事实')
    await startTicketAnalysis(updated)
  } catch (error) {
    showToast(`补录失败：${(error as Error).message}`)
  } finally {
    orderSaving.value = false
  }
}

function askCustomerForOrder(): void {
  missingOrderVisible.value = false
  showToast('请先向客户索取订单号；收到后回到本工单补录')
}

async function handoffMissingOrder(): Promise<void> {
  missingOrderVisible.value = false
  await askAssign()
}

async function askAssign(): Promise<void> {
  actionKind.value = 'assign'
  actionTitle.value = '转派工单'
  actionHint.value = '选择负责人并说明转派原因；涉及退款、责任判断或风控的工单建议转给有审批权的主管。'
  actionReason.value = ''
  actionAssignee.value = ''
  assignmentRecommendations.value = []
  recommendationRuleVersion.value = ''
  await Promise.all([loadAssignees(), loadAssignmentRecommendations()])
  actionVisible.value = true
}

/** 拉转派候选人（失败不阻塞弹窗：至少还能看理由输入框，不至于点不开） */
async function loadAssignees(): Promise<void> {
  try {
    const response = await ticketApi.assignees()
    assignees.value = response.items
  } catch {
    assignees.value = []
  }
}

async function loadAssignmentRecommendations(): Promise<void> {
  const ticket = currentTicket.value
  if (!ticket) return
  assignmentRecommendationsLoading.value = true
  try {
    const response = await ticketApi.assignmentRecommendations(ticket.id)
    assignmentRecommendations.value = response.items
    recommendationRuleVersion.value = response.rule_version
    if (response.items.length) actionAssignee.value = response.items[0].user_id
  } catch {
    assignmentRecommendations.value = []
    recommendationRuleVersion.value = ''
  } finally {
    assignmentRecommendationsLoading.value = false
  }
}

function chooseRecommendedAssignee(item: TicketAssignmentCandidate): void {
  actionAssignee.value = item.user_id
  if (!actionReason.value.trim()) {
    actionReason.value = `按派单推荐选择：${item.reasons.join('；')}`
  }
}

function askEscalate(): void {
  actionKind.value = 'escalate'
  actionTitle.value = '升级处理'
  actionHint.value = '升级将通知售后主管并提高工单优先级。'
  actionReason.value = ''
  actionVisible.value = true
}

function askDecision(decision: 'approve' | 'reject'): void {
  actionKind.value = decision
  actionTitle.value = decision === 'approve' ? '主管审批' : '驳回建议'
  actionHint.value = '请核对证据与处理方案；涉及退款、责任判断或风控的建议必须填写审批理由。'
  actionReason.value = ''
  actionVisible.value = true
}

function askSend(): void {
  const row = recommendation.value
  const staffConfirm = row?.approval_requirement === 'staff_confirm'
  actionKind.value = 'send'
  actionTitle.value = staffConfirm ? '客服确认并发送' : '确认发送回复'
  actionHint.value = staffConfirm
    ? '这是需要客服确认的处理动作：你确认后系统会记录确认人和理由，再执行并发送回复。'
    : '发送内容将写入工单消息，并记录操作者和时间。'
  actionReason.value = staffConfirm ? '客服确认后执行' : '按建议发送'
  actionVisible.value = true
}

function applyQuickReply(content: string): void {
  replyDraft.value = renderTemplate(content)
  if (replyDraft.value.includes('{{')) return
  showToast('已套用回复模板，可继续修改')
}

function replyCommitmentWarnings(): string[] {
  const row = recommendation.value
  if (!row || !replyDraft.value.trim()) return ['回复内容不能为空']
  const warnings: string[] = []
  if (row.approval_requirement === 'supervisor' || row.approval_requirement === 'risk_control') {
    if (row.status !== 'approved' && row.status !== 'sent') warnings.push('当前建议尚未审批，不能发送涉及退款、赔付或责任判断的回复')
  }
  if (/已为您发起退款|将为您发起退款|预计\s*\d+\s*[-到至]\s*\d+\s*个工作日到账|预计\s*\d+\s*个工作日到账/.test(replyDraft.value)) {
    warnings.push('回复中包含确定性退款或到账承诺，请改为审核/核实中的表述')
  }
  return warnings
}

async function sendInternalNote(): Promise<void> {
  const ticket = currentTicket.value
  const content = noteDraft.value.trim()
  if (!ticket || !content) {
    showToast('请填写内部备注')
    return
  }
  messageSending.value = true
  try {
    await ticketApi.reply(ticket.id, {
      content,
      visible_to_customer: false,
      message_type: 'internal_note',
      mention_user_ids: noteMentionIds.value,
      idempotency_key: `ui-note-${ticket.id}-${Date.now()}`,
    })
    noteDraft.value = ''
    noteMentionIds.value = []
    showToast('内部备注已记录，客户不可见')
    await refreshDetail()
  } catch (error) {
    showToast(`备注失败：${(error as Error).message}`)
  } finally {
    messageSending.value = false
  }
}

function askCreate(): void {
  actionKind.value = 'create'
  actionTitle.value = '新建工单'
  actionHint.value = '录入用户问题与订单号（订单号缺失会导致无法查询订单事实）。'
  actionReason.value = ''
  createVisible.value = true
}

async function confirmAction(): Promise<void> {
  const ticket = currentTicket.value
  const reason = actionReason.value.trim()
  if (actionKind.value !== 'create' && actionKind.value !== 'analyze' && !reason) {
    showToast('请填写本次操作原因')
    return
  }
  if (actionKind.value === 'assign' && !actionAssignee.value) {
    showToast('请选择负责人')
    return
  }
  try {
    if (actionKind.value === 'analyze' && ticket) {
      await startTicketAnalysis(ticket)
      return
    }
    if (actionKind.value === 'assign' && ticket) {
      await ticketApi.assign(ticket.id, { assignee_id: actionAssignee.value, reason })
      showToast('转派已记录')
    } else if (actionKind.value === 'escalate' && ticket) {
      await ticketApi.transition(ticket.id, { target: 'escalated', reason })
      showToast('升级处理已记录')
    } else if (actionKind.value === 'approve' && recommendation.value) {
      await ticketApi.decide(recommendation.value.id, {
        decision: 'approve',
        reason,
        modified_reply: replyDraft.value || undefined,
        idempotency_key: `ui-approve-${recommendation.value.id}`,
      })
      showToast('审批处理建议已记录')
    } else if (actionKind.value === 'reject' && recommendation.value) {
      await ticketApi.decide(recommendation.value.id, { decision: 'reject', reason })
      showToast('已驳回，工单回到分析中')
    } else if (actionKind.value === 'send' && recommendation.value) {
      const warnings = replyCommitmentWarnings()
      if (warnings.length) {
        showToast(warnings[0])
        return
      }
      await ticketApi.send(recommendation.value.id, {
        content: replyDraft.value.trim(),
        idempotency_key: `ui-send-${recommendation.value.id}-${Date.now()}`,
      })
      showToast('确认发送回复已记录')
    }
    actionVisible.value = false
    await refreshDetail()
    await loadTickets()
    await loadStats()
  } catch (error) {
    showToast(`操作失败：${(error as Error).message}`)
  }
}

async function submitCreate(): Promise<void> {
  if (!createForm.value.subject.trim() || !createForm.value.content.trim()) {
    showToast('请填写问题摘要与用户原话')
    return
  }
  // 风控标记与敏感客户是 L3/L4 的判据：界面上不提供入口的话，L4（转风控）这条路
  // 在界面里根本造不出来（只能靠评测脚本），演示与联调都验不了。
  const attributes: Record<string, unknown> = {}
  if (createForm.value.risk_flags.length) attributes.risk_flags = [...createForm.value.risk_flags]
  if (createForm.value.sensitive_customer) attributes.sensitive_customer = true
  try {
    const created = await ticketApi.create({
      subject: createForm.value.subject,
      content: createForm.value.content,
      order_id: createForm.value.order_id || undefined,
      customer_id: createForm.value.customer_id || undefined,
      priority: createForm.value.priority,
      attributes: Object.keys(attributes).length ? attributes : undefined,
      idempotency_key: `ui-create-${createForm.value.subject}-${Date.now()}`,
    })
    createVisible.value = false
    showToast('工单已创建')
    createForm.value = {
      subject: '',
      content: '',
      order_id: '',
      customer_id: '',
      priority: 'medium',
      risk_flags: [],
      sensitive_customer: false,
    }
    await loadTickets()
    await loadStats()
    await openTicket(created)
  } catch (error) {
    showToast(`创建失败：${(error as Error).message}`)
  }
}

function copyReply(): void {
  void navigator.clipboard?.writeText(replyDraft.value || '')
  showToast('回复草稿已复制')
}

/**
 * 导出当前筛选结果（真 CSV，Excel 可直接打开）。
 *
 * 原来只弹一句"演示环境仅提示"—— 客服主管最常用的动作就是把这批工单导出去排班/复盘，
 * 一个不产出文件的导出按钮等于没有。这里改为：按当前筛选条件**再取一页**（最多 200 条，
 * 与后端一致），生成带 BOM 的 CSV 并触发下载。
 */
async function exportList(): Promise<void> {
  try {
    const response = await ticketApi.list({
      status: statusFilter.value.length ? statusFilter.value : undefined,
      issue_type: issueFilter.value.length ? issueFilter.value : undefined,
      priority: priorityFilter.value.length ? priorityFilter.value : undefined,
      scope: ticketScope.value,
      attention: attentionQueue.value || undefined,
      keyword: keyword.value || undefined,
      include_test: includeTestRecords.value || undefined,
      page: 1,
      page_size: 200,
    })
    const rows = response.items ?? []
    if (!rows.length) {
      showToast('当前筛选结果为空，没有可导出的内容')
      return
    }
    const header = [
      '工单号',
      '问题摘要',
      '问题类型',
      '优先级',
      '状态',
      '风险等级',
      '负责人',
      '客户',
      '订单号',
      '渠道',
      '创建时间',
      '更新时间',
    ]
    const escape = (value: unknown): string => {
      const text = value === null || value === undefined ? '' : String(value)
      // CSV 注入防护：忽略前置空白/控制字符后检查公式前缀。
      const safe = /^[\t\r\n \x00-\x1f\x7f\uFEFF]*[=+\-@]/.test(text) ? `'${text}` : text
      return `"${safe.replace(/"/g, '""')}"`
    }
    const lines = [header.map(escape).join(',')]
    for (const row of rows) {
      lines.push(
        [
          row.external_id,
          row.subject,
          issueText(row.issue_type),
          priorityText(row.priority),
          statusText(row.status),
          RISK_TIER_TEXT[row.risk_tier ?? ''] ?? '需要人工判断',
          row.assignee_id ?? '未分配',
          row.customer_id,
          row.order_id,
          channelText(row.channel),
          row.created_at?.replace('T', ' ').slice(0, 19),
          row.updated_at?.replace('T', ' ').slice(0, 19),
        ]
          .map(escape)
          .join(','),
      )
    }
    // BOM 不能少：没有它 Excel 打开中文是乱码
    const blob = new Blob([`\uFEFF${lines.join('\r\n')}`], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `售后工单-${new Date().toISOString().slice(0, 10)}.csv`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
    const totalCount = response.meta?.total ?? rows.length
    // 说实话：后端单页上限 200 条，筛选结果更多时只导出了前 200 条，不能让人以为导全了
    if (totalCount > rows.length) {
      showToast(`已导出前 ${rows.length} 条（符合筛选的共 ${totalCount} 条，请缩小筛选范围再导）`)
    } else {
      showToast(`已导出 ${rows.length} 条工单（CSV）`)
    }
  } catch (error) {
    showToast(`导出失败：${(error as Error).message}`)
  }
}

onMounted(async () => {
  const requests = [loadStats(), loadTickets(), loadAssignees(), loadTemplates(), loadMentions()]
  if (canViewTechnicalDetails.value) requests.push(loadPolicies())
  await Promise.all(requests)
})

onUnmounted(() => {
  streamAbort?.abort()
  if (toastTimer) window.clearTimeout(toastTimer)
})
</script>

<template>
  <div class="tk">
    <div class="tk__head">
      <div>
        <h2>{{ workbenchTitle }}</h2>
        <p class="pic-muted">先看需要你处理的工单，再按“下一步”完成回复、补充或审批。</p>
      </div>
      <div class="tk__head-actions">
        <el-button @click="openMentionPanel">待我关注<span v-if="unreadMentionCount" class="mention-count">{{ unreadMentionCount }}</span></el-button>
        <el-button v-if="canManageTemplates" @click="openTemplateManager">管理回复模板</el-button>
        <el-button @click="exportList">导出列表</el-button>
        <el-button type="primary" @click="askCreate">＋ 新建工单</el-button>
      </div>
    </div>

    <div class="tk__stats" role="group" aria-label="工单行动队列">
      <button type="button" :class="['stat', { 'stat--active': attentionQueue === 'at_risk' }]" :aria-pressed="attentionQueue === 'at_risk'" @click="selectAttention('at_risk')">
        <span class="stat__label">即将超时</span>
        <strong class="stat__value stat__value--risk">{{ stats?.at_risk ?? 0 }}</strong>
        <span class="stat__hint stat__hint--risk">请优先处理</span>
      </button>
      <button type="button" :class="['stat', { 'stat--active': attentionQueue === 'waiting_reply' }]" :aria-pressed="attentionQueue === 'waiting_reply'" @click="selectAttention('waiting_reply')">
        <span class="stat__label">等待我回复</span>
        <strong class="stat__value">{{ stats?.waiting_reply ?? 0 }}</strong>
        <span class="stat__hint">事实已经齐全</span>
      </button>
      <button type="button" :class="['stat', { 'stat--active': attentionQueue === 'waiting_customer' }]" :aria-pressed="attentionQueue === 'waiting_customer'" @click="selectAttention('waiting_customer')">
        <span class="stat__label">等待客户补充</span>
        <strong class="stat__value">{{ stats?.waiting_customer ?? 0 }}</strong>
        <span class="stat__hint">无需反复查看</span>
      </button>
      <button type="button" :class="['stat', { 'stat--active': attentionQueue === 'waiting_review' }]" :aria-pressed="attentionQueue === 'waiting_review'" @click="selectAttention('waiting_review')">
        <span class="stat__label">待主管审批</span>
        <strong class="stat__value">{{ stats?.waiting_review ?? 0 }}</strong>
        <span class="stat__hint">审批后自动提醒</span>
      </button>
      <button type="button" :class="['stat', { 'stat--active': attentionQueue === 'resolved_today' }]" :aria-pressed="attentionQueue === 'resolved_today'" @click="selectAttention('resolved_today')">
        <span class="stat__label">今日已完成</span>
        <strong class="stat__value">{{ stats?.resolved_today ?? 0 }}</strong>
        <span class="stat__hint">已发送或已关闭</span>
      </button>
    </div>
    <p class="queue-caption">点选上方队列可按处理时限筛选；每条工单都会标出当前状态和建议的下一步。</p>

    <div v-if="attentionQueue" class="queue-filter" role="status" aria-live="polite" aria-atomic="true">
      <span>当前队列：<strong>{{ attentionLabel }}</strong></span>
      <el-button text type="primary" @click="attentionQueue = null; page = 1; loadTickets()">查看全部</el-button>
    </div>

    <div class="tk__filters">
      <el-segmented
        v-model="ticketScope"
        aria-label="工单范围"
        :options="[
          { label: '全部工单', value: 'all' },
          { label: '我的工单', value: 'mine' },
          { label: '未分配', value: 'unassigned' },
        ]"
        @change="changeScope"
      />
      <el-input v-model="keyword" aria-label="搜索工单" placeholder="搜索工单号、订单号或问题关键词" clearable style="width: 260px" @keyup.enter="page = 1; loadTickets()" />
      <details class="advanced-filters">
        <summary>更多筛选<span v-if="advancedFilterCount" class="advanced-filters__count">{{ advancedFilterCount }}</span></summary>
        <div class="advanced-filters__body">
          <el-select v-model="statusFilter" aria-label="按状态筛选" multiple collapse-tags clearable placeholder="全部状态" style="width: 190px" @change="page = 1; loadTickets()">
            <el-option v-for="item in stats?.status_options ?? []" :key="item" :label="statusText(item)" :value="item" />
          </el-select>
          <el-select v-model="issueFilter" aria-label="按问题类型筛选" multiple collapse-tags clearable placeholder="全部问题类型" style="width: 200px" @change="page = 1; loadTickets()">
            <el-option v-for="item in stats?.issue_type_options ?? []" :key="item" :label="issueText(item)" :value="item" />
          </el-select>
          <el-select v-model="priorityFilter" aria-label="按优先级筛选" multiple collapse-tags clearable placeholder="全部优先级" style="width: 170px" @change="page = 1; loadTickets()">
            <el-option v-for="item in ['urgent', 'high', 'medium', 'low']" :key="item" :label="priorityText(item)" :value="item" />
          </el-select>
          <el-checkbox v-model="includeTestRecords" @change="page = 1; loadTickets()">
            显示测试记录<span v-if="stats?.excluded_test_records">（{{ stats.excluded_test_records }}）</span>
          </el-checkbox>
          <el-button text type="primary" @click="resetFilters">重置筛选</el-button>
        </div>
      </details>
    </div>

    <div class="list-section-head">
      <div>
        <h3>工单列表</h3>
        <p>打开一单后，按“客户反馈 → 核实信息 → 处理建议 → 处理记录”查看。</p>
      </div>
      <span>{{ total }} 条</span>
    </div>

    <el-table
      v-loading="loading"
      :data="tickets"
      style="width: 100%"
      row-key="id"
      @row-click="openTicket"
    >
      <el-table-column label="客户问题" min-width="300">
        <template #default="{ row }">
          <div class="cell-strong">{{ row.subject }}</div>
          <div class="cell-sub">
            #{{ (row.external_id || row.id).slice(-8) }} · 订单 {{ row.order_id || '待补充' }}
            <span v-if="row.data_origin === 'demo'" class="tag-mini">演示数据</span>
            <span v-else-if="row.data_origin === 'test'" class="tag-mini tag-mini--warn">测试数据</span>
          </div>
          <div class="ticket-meta">
            <span class="ticket-meta__issue">{{ issueText(row.issue_type) }}</span>
            <span :class="`prio prio--${row.priority}`">{{ priorityText(row.priority) }}优先级</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="处理进度" min-width="190">
        <template #default="{ row }">
          <span :class="statusClass(row.status)">{{ statusText(row.status, row.missing_fields) }}</span>
          <div :class="['sla-text', `sla-text--${row.sla_status || 'on_track'}`]">
            {{ row.sla_status === 'completed' ? '—' : row.sla_remaining_seconds != null ? (row.sla_remaining_seconds < 0 ? `已超时 ${Math.abs(Math.floor(row.sla_remaining_seconds / 60))} 分钟` : `${Math.max(1, Math.floor(row.sla_remaining_seconds / 60))} 分钟`) : '—' }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="下一步做什么" min-width="190">
        <template #default="{ row }">
          <button
            type="button"
            class="next-action"
            data-ticket-action="open-detail"
            :aria-label="`查看工单 ${row.subject} 详情，下一步：${row.next_action || '查看工单'}`"
            @keydown.enter.stop.prevent="openTicket(row)"
            @keydown.space.stop.prevent="openTicket(row)"
            @click.stop="openTicket(row)"
          >
            {{ row.next_action || '查看工单' }} →
          </button>
        </template>
      </el-table-column>
      <el-table-column label="处理人" min-width="150">
        <template #default="{ row }">
          <div :class="{ 'pic-muted': !row.assignee_id }">{{ row.handler_label || assigneeText(row.assignee_id) }}</div>
          <div class="cell-sub">更新于 {{ relativeTime(row.updated_at) }}</div>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty :description="attentionQueue ? `${attentionLabel}队列暂时没有工单` : '没有符合条件的工单'" />
      </template>
    </el-table>

    <div class="tk__pager">
      <span class="pic-muted">显示 {{ tickets.length }} 条，共 {{ total }} 条</span>
      <el-pagination
        v-model:current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="prev, pager, next"
        @current-change="loadTickets"
      />
    </div>

    <!-- 工单详情：先给下一步，再按客户反馈、核实信息、建议和处理记录分区 -->
    <el-drawer
      v-model="drawerOpen"
      class="ticket-detail-drawer"
      :size="'min(980px, 94vw)'"
      direction="rtl"
    >
      <template #header>
        <div class="drawer-head">
          <el-button text @click="drawerOpen = false">← 返回</el-button>
          <strong>{{ detail?.ticket.external_id || detail?.ticket.id?.slice(-8) || '工单' }}</strong>
          <span v-if="currentTicket" :class="statusClass(currentTicket.status)">
            {{ statusText(currentTicket.status, currentTicket.missing_fields) }}
          </span>
          <span style="flex: 1" />
          <el-button size="small" @click="askAssign">转派</el-button>
          <el-button size="small" @click="askEscalate">升级处理</el-button>
          <el-button size="small" @click="askAnalyze">
            {{ currentTicket?.analysis_count ? '重新分析' : '启动分析' }}
          </el-button>
        </div>
      </template>

      <div v-loading="detailLoading" class="drawer-body">
        <template v-if="detail">
          <section class="next-step-banner col--full" aria-label="本单下一步">
            <span class="next-step-banner__label">本单下一步</span>
            <strong>{{ detail.handoff_summary?.next_step || currentTicket?.next_action || '先核对工单信息，再确定处理方案' }}</strong>
            <span v-if="recommendation?.write_action_blocked" class="next-step-banner__guard">
              尚未审批，不可执行退款或赔付
            </span>
          </section>

          <div class="col">
            <div class="card">
              <div class="card__title">
                <h3><span class="section-number">01</span>客户反馈</h3>
                <span v-if="currentTicket" :class="`prio prio--${currentTicket.priority}`">
                  {{ priorityText(currentTicket.priority) }}优先级
                </span>
              </div>
              <p class="card__intro">以下是客户提交的问题原文，用来确认客户诉求，不代表系统已核实。</p>
              <div class="quote">{{ detail.ticket.content }}</div>
            </div>

            <div class="card">
              <div class="card__title">
                <h3><span class="section-number">02</span>已核实的信息</h3>
                <span v-if="detail.fact_rows.some((row) => row.is_mock)" class="tag-mini">含演示数据</span>
              </div>
              <p class="card__intro">这里只展示已查到的订单、支付或物流记录；缺失项会明确标出，不会当作“没有问题”。</p>
              <dl class="kv">
                <template v-for="row in detail.fact_rows" :key="row.key">
                  <dt>{{ row.label }}</dt>
                  <dd>
                    {{ row.value ?? '—' }}
                    <span v-if="row.is_mock" class="tag-mini">模拟</span>
                    <span v-if="row.stale" class="tag-mini tag-mini--warn">可能过期</span>
                  </dd>
                </template>
                <dt>工单来源</dt>
                <dd>{{ channelText(detail.ticket.channel) }}</dd>
                <template v-if="!detail.fact_rows.length">
                  <dt>订单事实</dt>
                  <dd class="pic-muted">尚未查询（点击「启动分析」会先补事实）</dd>
                </template>
              </dl>
              <div
                v-if="detail.ticket.missing_fields.length"
                class="handoff-missing"
                role="status"
              >
                {{ detail.ticket.missing_fields.some((item) => item.includes('事实来源暂不可用'))
                  ? `当前受阻：${detail.ticket.missing_fields.join('；')}`
                  : `待补信息：${detail.ticket.missing_fields.join('；')}` }}
              </div>
            </div>
          </div>

          <div class="col">
            <div class="card">
              <div class="card__title">
                <h3><span class="section-number">03</span>建议处理</h3>
                <span v-if="recommendation" :class="['pill', recommendation.answerability_code === 'ready_to_reply' ? 'pill--done' : recommendation.answerability_code === 'need_information' ? 'pill--todo' : 'pill--warn']">
                  {{ recommendation.answerability_label || '待评估' }}
                </span>
              </div>
              <p class="card__intro">这是系统根据当前信息整理的处理建议，发送或执行前仍需按页面提示由客服或主管确认。</p>

              <div v-if="recommendation" class="answerability-box">
                <strong>{{ recommendation.answerability_label }}</strong>
                <span>{{ recommendation.answerability_reason }}</span>
              </div>

              <section v-if="detail.analysis?.mode" class="business-collaboration" aria-label="本次协作处理过程">
                <div class="business-collaboration__head">
                  <span class="business-collaboration__mode">
                    本次由 {{ detail.analysis.agent_count }} 个业务角色协作
                  </span>
                </div>
                <p class="business-collaboration__summary">
                  {{ businessRoleSummary || detail.analysis.mode_label }}
                </p>
                <div
                  v-if="detail.analysis.reflection"
                  :class="[
                    'reflection-summary',
                    detail.analysis.reflection.verdict === 'human_review'
                      ? 'reflection-summary--warn'
                      : 'reflection-summary--ok',
                  ]"
                  role="status"
                >
                  <div>
                    <strong>{{ detail.analysis.reflection.label }}</strong>
                    <span v-if="detail.analysis.reflection.enabled">
                      共复核 {{ detail.analysis.reflection.rounds }} 次
                    </span>
                  </div>
                  <details v-if="detail.analysis.reflection.issues.length">
                    <summary>查看发现的问题</summary>
                    <ul>
                      <li v-for="issue in detail.analysis.reflection.issues" :key="issue.code">
                        {{ issue.message }}
                      </li>
                    </ul>
                  </details>
                </div>
                <details class="business-collaboration__details">
                  <summary>展开查看各角色做了什么</summary>
                  <p class="business-collaboration__reason">{{ detail.analysis.route_reason }}</p>
                  <div v-if="detail.analysis.applied_skills?.length" class="business-collaboration__skills">
                    <strong>本次核验重点</strong>
                    <span>{{ detail.analysis.applied_skills.map((skill) => BUSINESS_SKILL_TEXT[skill] || '通用证据检查').join(' · ') }}</span>
                  </div>
                  <div class="business-collaboration__steps">
                    <article v-for="step in businessSteps" :key="step.agent_code" class="business-step">
                      <span :class="['business-step__status', `business-step__status--${step.status}`]">
                        {{ businessStepStatusText(step.status) }}
                      </span>
                      <div class="business-step__content">
                        <div class="business-step__heading">
                          <strong>{{ step.role }}</strong>
                          <span class="business-step__parallel">
                            {{ step.parallel_group ? `${step.parallel_group}（并行）` : step.phase }}
                          </span>
                        </div>
                        <p>{{ step.responsibility }}</p>
                        <small>{{ step.why }}</small>
                        <small>{{ step.conclusion }}</small>
                      </div>
                    </article>
                  </div>
                </details>
              </section>

              <details v-if="canViewTechnicalDetails && steps.length" class="technical-details">
                <summary>查看技术核验过程</summary>
                <div class="agent-line" v-for="step in steps" :key="step.agent_code">
                <span class="agent-line__icon">{{ step.short }}</span>
                <div class="agent-line__body">
                  <div class="cell-strong">{{ step.name }}</div>
                  <div class="cell-sub">
                    {{ step.error ? `失败：${step.error}` : `耗时 ${step.latency_ms ?? 0} ms · ${step.total_tokens ?? 0} tokens` }}
                  </div>
                </div>
                <span :class="step.status === 'succeeded' ? 'ok' : step.status === 'failed' ? 'fail' : 'run'">
                  {{ step.status === 'succeeded' ? '完成' : step.status === 'failed' ? '失败' : '进行中' }}
                </span>
                </div>
              </details>
              <div v-else-if="!recommendation" class="pic-muted">尚未分析：点右上「启动分析」。</div>

              <template v-if="streaming">
                <div class="business-progress">
                  <el-icon class="is-loading"><Loading /></el-icon>
                  <div>
                    <strong>正在核实订单与处理规则</strong>
                    <span>完成后会自动刷新处理建议，请稍候。</span>
                  </div>
                </div>
                <div v-if="canViewTechnicalDetails" class="log">
                  <div v-for="(item, index) in progress" :key="index" class="log__line">
                    <span class="pic-mono">{{ item.node }}</span> {{ item.message }}
                  </div>
                </div>
                <div v-else-if="progress.length" class="business-progress__updates" aria-live="polite">
                  <p v-for="(item, index) in progress.slice(-3)" :key="index">{{ item.message }}</p>
                </div>
              </template>

              <template v-if="recommendation">
                <h4>建议处理方案</h4>
                <div class="action">
                  <strong>{{ ACTION_TEXT[recommendation.action_type] ?? recommendation.action_type }}</strong>
                  <span v-if="recommendation.amount && String(recommendation.amount) !== '0'">
                    · 金额 {{ recommendation.amount }} {{ recommendation.currency }}
                    <!-- 金额来源：审批人要能一眼看出这笔钱是"订单实付"还是"政策补偿标准" -->
                    <span v-if="recommendation.amount_source" class="pic-muted">
                      （来源：{{ recommendation.amount_source }}）
                    </span>
                  </span>
                  <!-- 不涉及金额时也要说清"为什么没有钱"（规则判断依据），否则审批人会以为漏算了 -->
                  <span v-else-if="recommendation.amount_source" class="pic-muted">
                    · {{ recommendation.amount_source }}
                  </span>
                </div>
                <p class="pic-muted">{{ recommendation.rationale }}</p>

                <div v-if="recommendation.policy_refs.length" class="evidence">
                  <div class="evidence__title">
                    政策依据 · {{ recommendation.policy_refs[0].title }}
                    <span v-if="recommendation.policy_refs[0].version">
                      {{ recommendation.policy_refs[0].version }}
                    </span>
                    <span v-if="recommendation.policy_refs[0].clause">
                      · {{ recommendation.policy_refs[0].clause }}
                    </span>
                  </div>
                  <div class="evidence__quote">“{{ recommendation.policy_refs[0].snippet }}”</div>
                </div>
                <div v-else class="pic-muted">未命中政策依据 —— 该建议不应直接作为处理依据。</div>

                <div class="risk-box">
                  <strong>{{ riskText() }}</strong>
                  <div>{{ recommendation.approval_label }}</div>
                  <div v-if="recommendation.write_action_blocked">
                    此建议尚未批准，禁止执行退款或赔付。
                  </div>
                  <div v-for="warning in recommendation.warnings" :key="warning" class="risk-box__warn">
                    · {{ warning }}
                  </div>
                </div>

                <div v-if="detail.handoff_summary" class="handoff-box">
                  <div class="card__title">
                    <strong>交接摘要</strong>
                    <span class="pic-muted">接手人先看这里</span>
                  </div>
                  <div class="handoff-grid">
                    <span>当前状态</span><strong>{{ detail.handoff_summary.status_label }}</strong>
                    <span>建议动作</span><strong>{{ detail.handoff_summary.suggested_action }}</strong>
                    <span>下一步</span><strong>{{ detail.handoff_summary.next_step }}</strong>
                  </div>
                  <div v-if="detail.handoff_summary.known_facts.length" class="pic-muted">已知事实：{{ detail.handoff_summary.known_facts.join('；') }}</div>
                  <div v-if="detail.handoff_summary.missing_information.length" class="handoff-missing">待补信息：{{ detail.handoff_summary.missing_information.join('、') }}</div>
                </div>

                <h4>给客户的回复草稿</h4>
                <div class="quick-replies">
                  <span class="pic-muted">常用回复：</span>
                  <el-button v-for="item in quickReplies" :key="item.id" size="small" text @click="applyQuickReply(item.content)">{{ item.title }}</el-button>
                </div>
                <el-input v-model="replyDraft" aria-label="消费者回复草稿" type="textarea" :rows="5" maxlength="10000" show-word-limit />
                <div v-if="replyCommitmentWarnings().length" class="reply-warning">
                  {{ replyCommitmentWarnings()[0] }}
                </div>
                <div class="card__actions">
                  <el-button size="small" @click="copyReply">复制回复</el-button>
                  <el-button size="small" @click="askAssign">转派</el-button>
                  <template v-if="recommendation.status === 'pending_approval'">
                    <el-button size="small" @click="askDecision('reject')">驳回建议</el-button>
                    <el-button size="small" type="primary" @click="askDecision('approve')">
                      主管审批
                    </el-button>
                  </template>
                  <el-button
                    v-else
                    size="small"
                    type="primary"
                    :disabled="recommendation.write_action_blocked"
                    @click="askSend"
                  >
                    {{
                      recommendation.approval_requirement === 'staff_confirm'
                        ? '客服确认并发送'
                        : '确认并发送回复'
                    }}
                  </el-button>
                </div>
              </template>
            </div>
          </div>

          <section class="card col--full handling-records" aria-label="处理记录">
            <div class="card__title">
              <h3><span class="section-number">04</span>处理记录</h3>
              <span class="pic-muted">记录系统处理进度与内部沟通；内部备注不会发送给客户</span>
            </div>
            <div class="handling-records__grid">
              <section class="handling-records__block" aria-label="处理时间线">
                <h4>进度时间线</h4>
                <el-timeline v-if="detail.timeline.length">
                  <el-timeline-item
                    v-for="item in detail.timeline"
                    :key="`${item.at}-${item.kind}`"
                    :timestamp="item.at?.replace('T', ' ').slice(0, 19)"
                    size="small"
                  >
                    <div class="cell-strong">{{ item.title }}</div>
                    <div v-if="item.detail" class="cell-sub">{{ item.detail }}</div>
                  </el-timeline-item>
                </el-timeline>
                <p v-else class="pic-muted">还没有处理记录。</p>
              </section>
              <section class="handling-records__block" aria-label="内部协作">
                <h4>内部协作 <span class="pic-muted">仅团队可见</span></h4>
                <div v-if="detail.messages.some((message) => message.meta?.message_type === 'internal_note')" class="internal-note-list">
                  <div
                    v-for="message in detail.messages.filter((item) => item.meta?.message_type === 'internal_note')"
                    :key="message.id"
                    class="internal-note"
                  >
                    <strong>内部备注</strong>
                    <span>{{ message.content }}</span>
                    <small v-if="message.meta?.mention_user_ids?.length">已提醒 {{ message.meta.mention_user_ids.length }} 位协作者</small>
                  </div>
                </div>
                <el-input v-model="noteDraft" aria-label="内部备注内容" type="textarea" :rows="2" maxlength="10000" show-word-limit placeholder="写内部备注，客户不可见" />
                <div class="note-actions">
                  <el-select v-model="noteMentionIds" aria-label="选择需要提醒的主管或协作者" multiple collapse-tags placeholder="提醒主管/协作者" style="min-width: 180px">
                    <el-option
                      v-for="item in assignees.filter((candidate) => candidate.can_approve)"
                      :key="item.id"
                      :label="item.full_name || item.username"
                      :value="item.id"
                    />
                  </el-select>
                  <el-button size="small" type="primary" :loading="messageSending" @click="sendInternalNote">添加内部备注</el-button>
                </div>
              </section>
            </div>
          </section>

          <div v-if="canViewTechnicalDetails" class="col col--full">
            <div class="card card--audit">
              <details>
                <summary><strong>技术记录（仅用于排障）</strong></summary>
                <div class="pic-muted">
                Trace ID：{{ detail.analysis?.analysis_task_id || '—' }} · 工作流：after_sales ·
                分析次数：{{ detail.ticket.analysis_count }} · 每单成本：
                {{ detail.cost?.amount?.toFixed?.(5) ?? detail.cost?.amount ?? 0 }} 元
                （{{ detail.cost?.total_tokens ?? 0 }} tokens）
                </div>
                <div class="pic-muted">
                所有操作均记录操作者、时间、输入证据与决策理由；模拟数据已标注（is_mock）。
                </div>
              </details>
            </div>
          </div>

          <div v-if="canViewTechnicalDetails" class="col col--full">
            <div class="card">
              <h3>政策库现状（生效期过滤）</h3>
              <div class="policy-line" v-for="item in policies" :key="item.id">
                <span class="pill" :class="item.usable ? 'pill--done' : 'pill--risk'">
                  {{ item.usable ? '可用' : item.state === 'expired' ? '已失效' : item.state === 'future' ? '未生效' : '不可用' }}
                </span>
                <span class="cell-strong">{{ item.title }} {{ item.version }}</span>
                <span class="pic-muted">{{ item.reason }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>
    </el-drawer>

    <!-- 新建工单 -->
    <el-dialog v-model="createVisible" title="新建工单" width="560px">
      <el-form label-width="80px">
        <el-form-item label="问题摘要">
          <el-input v-model="createForm.subject" maxlength="255" show-word-limit placeholder="例如：包裹显示签收但未收到" />
        </el-form-item>
        <el-form-item label="用户原话">
          <el-input v-model="createForm.content" type="textarea" :rows="3" maxlength="20000" show-word-limit />
        </el-form-item>
        <el-form-item label="订单号">
          <el-input v-model="createForm.order_id" placeholder="TB202609241806（留空将无法查询订单事实）" />
        </el-form-item>
        <el-form-item label="客户 ID">
          <el-input v-model="createForm.customer_id" placeholder="U1001" />
        </el-form-item>
        <el-form-item label="优先级">
          <el-select v-model="createForm.priority" style="width: 160px">
            <el-option v-for="item in ['urgent', 'high', 'medium', 'low']" :key="item" :label="priorityText(item)" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item label="风控标记">
          <el-select
            v-model="createForm.risk_flags"
            multiple
            clearable
            placeholder="不选=无风控标记"
            style="width: 100%"
          >
            <el-option label="批量退款（batch_refund）" value="batch_refund" />
            <el-option label="账号受限（account_restriction）" value="account_restriction" />
            <el-option label="疑似欺诈（suspected_fraud）" value="suspected_fraud" />
          </el-select>
        </el-form-item>
        <el-form-item label="敏感客户">
          <el-switch v-model="createForm.sensitive_customer" />
          <span class="pic-muted" style="margin-left: 8px">
            打开后该单至少需要主管审批
          </span>
        </el-form-item>
        <el-form-item label="提示">
          <span class="pic-muted">
            勾选任一风控标记会将工单转人工风控，禁止自动执行；这是系统的安全规则。
          </span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" @click="submitCreate">创建并打开</el-button>
      </template>
    </el-dialog>

    <!-- 动作确认弹窗：理由必填 -->
    <el-dialog v-model="missingOrderVisible" title="先补齐订单号" width="500px">
      <el-alert
        title="当前工单没有订单号，系统无法核对支付、发货和物流事实。"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-form label-width="72px" style="margin-top: 18px">
        <el-form-item label="订单号">
          <el-input v-model="orderIdDraft" placeholder="粘贴订单号后可直接开始分析" @keyup.enter="saveOrderAndAnalyze" />
        </el-form-item>
      </el-form>
      <p class="pic-muted">如果暂时拿不到订单号，可先向客户索取，或转派给其他同事人工跟进。</p>
      <template #footer>
        <el-button @click="askCustomerForOrder">向客户索取</el-button>
        <el-button @click="handoffMissingOrder">转人工</el-button>
        <el-button type="primary" :loading="orderSaving" @click="saveOrderAndAnalyze">补录并分析</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="actionVisible" :title="actionTitle" width="470px" :show-close="false">
      <p class="pic-muted">{{ actionHint }}</p>
      <!-- 转派必须先选人：以前这里只有理由输入框、提交固定 assignee_id=null，
           结果负责人列永远是"未分配"，转派成了只写审计的空操作。 -->
      <el-form v-if="actionKind === 'assign'" label-width="72px" style="margin-bottom: 8px">
        <el-form-item label="智能推荐">
          <div v-loading="assignmentRecommendationsLoading" class="assign-recommendations">
            <button
              v-for="(item, index) in assignmentRecommendations"
              :key="item.user_id"
              type="button"
              :class="['assign-candidate', { 'assign-candidate--selected': actionAssignee === item.user_id }]"
              @click="chooseRecommendedAssignee(item)"
            >
              <span class="assign-candidate__head">
                <strong>{{ index === 0 ? '首选 · ' : '' }}{{ item.full_name || item.username }}</strong>
                <span>评分 {{ item.score }}</span>
              </span>
              <span class="assign-candidate__load">
                活跃 {{ item.active_ticket_count }} 单 · 高优 {{ item.high_priority_count }} 单
                <template v-if="item.can_approve"> · 可审批</template>
              </span>
              <span class="assign-candidate__reason">{{ item.reasons.join('；') }}</span>
            </button>
            <el-empty
              v-if="!assignmentRecommendationsLoading && !assignmentRecommendations.length"
              description="暂无可用推荐，请手动选择负责人"
              :image-size="48"
            />
            <small v-if="recommendationRuleVersion" class="pic-muted">
              推荐规则 {{ recommendationRuleVersion }}；仅供选择，不会自动改派
            </small>
          </div>
        </el-form-item>
        <el-form-item label="负责人">
          <el-select v-model="actionAssignee" placeholder="选择处理人" style="width: 100%">
            <el-option
              v-for="item in assignees"
              :key="item.id"
              :label="`${item.full_name || item.username}${item.can_approve ? '（可审批）' : ''}`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <el-input
        v-if="actionKind !== 'analyze'"
        v-model="actionReason"
        type="textarea"
        :rows="3"
        :placeholder="
          actionKind === 'assign'
            ? '填写转派原因（必填）'
            : actionKind === 'escalate'
              ? '填写升级原因（必填）'
              : actionKind === 'approve' || actionKind === 'reject'
                ? '填写审批理由（必填）'
                : '填写确认说明（必填）'
        "
      />
      <template #footer>
        <el-button @click="actionVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmAction">确认</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="mentionVisible" title="待我关注" width="560px">
      <div v-if="mentions.length" class="mention-list">
        <button v-for="item in mentions" :key="item.id" type="button" class="mention-item" @click="openMention(item)">
          <span class="mention-item__head">
            <strong>{{ item.subject }}</strong>
            <el-tag size="small" :type="item.read ? 'info' : 'warning'">{{ item.read ? '已读' : '待处理' }}</el-tag>
          </span>
          <span>{{ item.content }}</span>
          <small>{{ item.ticket_external_id || item.ticket_id.slice(-8) }} · {{ relativeTime(item.created_at) }}</small>
        </button>
      </div>
      <el-empty v-else description="暂无被 @ 的工单协作" :image-size="64" />
      <template #footer><el-button @click="mentionVisible = false">关闭</el-button></template>
    </el-dialog>

    <el-dialog v-if="canManageTemplates" v-model="templateVisible" title="管理回复模板" width="720px">
      <div class="template-manager">
        <div class="template-form">
          <el-input v-model="templateForm.title" maxlength="100" show-word-limit placeholder="模板名称，例如：等待物流核实" />
          <el-input v-model="templateForm.issue_type" placeholder="适用问题类型（可留空，例：logistics_stalled）" />
          <el-input v-model="templateForm.content" type="textarea" :rows="4" maxlength="5000" show-word-limit placeholder="模板内容，可使用 {{订单号}}、{{问题类型}}、{{金额}}" />
          <div class="pic-muted">模板只会套用成草稿，发送仍需通过审批和确定性承诺校验。</div>
          <div class="template-form__actions">
            <el-button v-if="templateEditing" @click="templateEditing = null; templateForm = { title: '', content: '', issue_type: '' }">取消编辑</el-button>
            <el-button type="primary" :loading="templateSaving" @click="saveTemplate">{{ templateEditing ? '保存修改' : '新增模板' }}</el-button>
          </div>
        </div>
        <div class="template-list">
          <div v-for="item in quickReplies" :key="item.id" class="template-row">
            <div>
              <strong>{{ item.title }}</strong>
              <span class="pic-muted">{{ item.issue_type || '全部问题类型' }}</span>
              <p>{{ item.content }}</p>
            </div>
            <div v-if="!item.id.startsWith('builtin-')" class="template-row__actions">
              <el-button link type="primary" @click="editTemplate(item)">编辑</el-button>
              <el-button link type="danger" @click="deactivateTemplate(item)">停用</el-button>
            </div>
          </div>
          <el-empty v-if="!quickReplies.length" description="还没有自定义模板" :image-size="56" />
        </div>
      </div>
    </el-dialog>

    <!-- 底部居中 Toast（原型约定，约 2.4 秒） -->
    <div v-if="toast.visible" class="toast" role="status" aria-live="polite" aria-atomic="true">{{ toast.text }}</div>
  </div>
</template>

<style scoped>
.tk {
  padding: 4px 2px 24px;
}
.tk__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.tk__head-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.mention-count {
  display: inline-flex;
  min-width: 18px;
  height: 18px;
  align-items: center;
  justify-content: center;
  margin-left: 5px;
  padding: 0 4px;
  border-radius: 9px;
  background: #b44141;
  color: #fff;
  font-size: 11px;
}
.tk__head h2 {
  margin: 0 0 4px;
  font-size: 20px;
}
.tk__stats {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  margin: 18px 0 16px;
  border: 1px solid var(--pic-line, #e1e5e9);
  border-radius: 6px;
  overflow: hidden;
}
.stat {
  appearance: none;
  width: 100%;
  padding: 14px 18px;
  border: 0;
  border-right: 1px solid var(--pic-line, #e1e5e9);
  background: #fff;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 4px;
  transition: background-color 0.16s ease, box-shadow 0.16s ease;
}
.stat:last-child {
  border-right: none;
}
.stat:hover {
  background: #f6faf8;
}
.stat:focus-visible {
  position: relative;
  z-index: 1;
  outline: 2px solid #187c67;
  outline-offset: -2px;
}
.stat--active {
  background: #edf7f3;
  box-shadow: inset 0 -3px 0 #187c67;
}
.stat__label {
  font-size: 12px;
  color: #5f6872;
}
.stat__value {
  font-size: 23px;
  font-weight: 650;
}
.stat__value--risk {
  color: #b44141;
}
.stat__hint {
  font-size: 12px;
  color: #68727f;
}
.stat__hint--risk {
  color: #b44141;
}
.queue-filter {
  display: flex;
  min-height: 34px;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: -6px 0 10px;
  padding: 4px 10px 4px 12px;
  border-left: 3px solid #187c67;
  background: #f4f8f6;
  color: #34433d;
  font-size: 13px;
}
.queue-caption {
  margin: -10px 0 14px;
  color: #68727f;
  font-size: 12px;
  line-height: 1.5;
}
.sla-text {
  font-weight: 600;
}
.sla-text--at_risk,
.sla-text--overdue {
  color: #b44141;
}
.sla-text--on_track {
  color: #187c67;
}
.next-action {
  appearance: none;
  min-height: 24px;
  padding: 2px 0;
  border: 0;
  background: transparent;
  color: #187c67;
  font: inherit;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
}
.next-action:hover {
  text-decoration: underline;
}
.next-action:focus-visible {
  border-radius: 3px;
  outline: 2px solid #187c67;
  outline-offset: 2px;
}
.answerability-box {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 10px 12px;
  margin: 8px 0 12px;
  border-radius: 5px;
  background: #f4f8f5;
  border: 1px solid #cfe2d8;
  color: #245c49;
}
.answerability-box span {
  color: #60726a;
  font-size: 12px;
}
.internal-note-list {
  display: grid;
  gap: 6px;
  margin-bottom: 8px;
}
.internal-note {
  display: grid;
  gap: 2px;
  padding: 8px 10px;
  border-left: 3px solid #c58c3c;
  background: #fff8ec;
  color: #634b2d;
  font-size: 12px;
}
.internal-note small { color: #967752; }
.mention-list,
.template-list {
  display: grid;
  gap: 8px;
  max-height: 420px;
  overflow: auto;
}
.mention-item {
  display: grid;
  gap: 4px;
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--pic-line, #e1e5e9);
  border-radius: 5px;
  background: #fff;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.mention-item:hover { border-color: var(--el-color-primary); background: #f7fbff; }
.mention-item__head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.mention-item small { color: #7b8590; }
.template-manager { display: grid; gap: 16px; grid-template-columns: minmax(250px, 0.9fr) minmax(300px, 1.1fr); }
.template-form { display: grid; gap: 9px; align-content: start; }
.template-form__actions { display: flex; justify-content: flex-end; gap: 8px; }
.template-row { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid #eef1f3; }
.template-row p { margin: 5px 0 0; color: #59636d; font-size: 12px; line-height: 1.55; }
.template-row .pic-muted { margin-left: 8px; font-size: 11px; }
.template-row__actions { white-space: nowrap; }
.note-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 8px;
}
.handoff-box {
  margin: 12px 0;
  padding: 10px 12px;
  border: 1px solid #cfe2d8;
  border-radius: 5px;
  background: #f7fbf8;
}
.handoff-grid {
  display: grid;
  grid-template-columns: 70px 1fr;
  gap: 5px 10px;
  margin: 8px 0;
  font-size: 12px;
}
.handoff-grid span { color: #718078; }
.handoff-missing { color: #a04f35; font-size: 12px; margin-top: 5px; }
.quick-replies {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 3px;
  margin: 6px 0;
}
.reply-warning {
  margin-top: 6px;
  padding: 7px 9px;
  border: 1px solid #efd0a9;
  border-radius: 4px;
  background: #fff7e9;
  color: #8b5b23;
  font-size: 12px;
}
.technical-details {
  margin: 8px 0 12px;
  color: #66716b;
  font-size: 12px;
}
.technical-details summary,
.card--audit summary {
  cursor: pointer;
  color: #68727f;
}
.business-progress {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 12px 0;
  padding: 12px;
  border: 1px solid #b9d8d0;
  border-radius: 6px;
  background: #f1f8f6;
  color: #245e52;
}
.business-progress .el-icon {
  flex: 0 0 auto;
  font-size: 20px;
}
.business-progress div {
  display: grid;
  gap: 2px;
}
.business-progress span {
  color: #55706a;
  font-size: 12px;
}
.business-collaboration {
  margin: 14px 0;
  padding-top: 12px;
  border-top: 1px solid var(--pic-line, #e1e5e9);
}
.business-collaboration__head,
.business-step__heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.business-collaboration__head h4 { margin: 0; }
.business-collaboration__mode { color: #315d51; font-size: 12px; font-weight: 600; }
.business-collaboration__summary {
  margin: 5px 0 7px;
  color: #5f6872;
  font-size: 12px;
  line-height: 1.5;
}
.reflection-summary {
  box-sizing: border-box;
  display: grid;
  gap: 7px;
  min-width: 0;
  max-width: 100%;
  margin: 8px 0 10px;
  padding: 9px 11px;
  border: 1px solid #cfe4dc;
  border-radius: 6px;
  background: #f4faf7;
  color: #315d51;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.reflection-summary--warn {
  border-color: #ecd4ae;
  background: #fff9ef;
  color: #89561a;
}
.reflection-summary > div {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 6px 12px;
}
.reflection-summary details > summary { cursor: pointer; font-weight: 600; }
.reflection-summary ul { margin: 7px 0 0; padding-left: 18px; }
.reflection-summary li + li { margin-top: 4px; }
.business-collaboration__details > summary {
  color: #187c67;
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
}
.business-collaboration__reason {
  margin: 6px 0 10px;
  color: #5f6872;
  font-size: 12px;
  line-height: 1.5;
}
.business-collaboration__skills {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.5;
}
.business-collaboration__skills strong { color: #46534d; }
.business-collaboration__skills span { color: #187c67; }
.business-collaboration__steps { display: grid; }
.business-step {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 9px;
  padding: 9px 0;
  border-top: 1px solid #eef1f3;
}
.business-step__status { align-self: start; color: #69727c; font-size: 11px; line-height: 1.5; }
.business-step__status--succeeded { color: #187c67; }
.business-step__status--running { color: #396d9d; }
.business-step__status--failed { color: #b44141; }
.business-step__content strong { font-size: 13px; }
.business-step__content p,
.business-step__content small {
  display: block;
  margin: 3px 0 0;
  color: #5f6872;
  font-size: 12px;
  line-height: 1.45;
}
.business-step__parallel { color: #396d9d; font-size: 11px; white-space: nowrap; }
.business-progress__updates p { margin: 5px 0 0; color: #5f6872; font-size: 12px; }
@media (prefers-reduced-motion: reduce) {
  .stat {
    transition: none;
  }
}
.tk__filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-bottom: 12px;
}
.advanced-filters {
  position: relative;
}
.advanced-filters > summary {
  display: inline-flex;
  min-height: 32px;
  align-items: center;
  gap: 6px;
  padding: 0 11px;
  border: 1px solid var(--pic-line, #dcdfe6);
  border-radius: 4px;
  background: #fff;
  color: #48545f;
  cursor: pointer;
  font-size: 13px;
  list-style: none;
}
.advanced-filters > summary::-webkit-details-marker {
  display: none;
}
.advanced-filters > summary::after {
  content: '⌄';
  color: #7b8590;
}
.advanced-filters[open] > summary {
  border-color: #8ebcaf;
  color: #187c67;
}
.advanced-filters[open] > summary::after {
  transform: rotate(180deg);
}
.advanced-filters__count {
  min-width: 18px;
  padding: 1px 5px;
  border-radius: 9px;
  background: #e5f3ef;
  color: #187c67;
  font-size: 11px;
  text-align: center;
}
.advanced-filters__body {
  display: flex;
  flex-wrap: wrap;
  gap: 9px;
  align-items: center;
  width: min(720px, calc(100vw - 64px));
  margin-top: 8px;
  padding: 10px;
  border: 1px solid var(--pic-line, #e1e5e9);
  border-radius: 6px;
  background: #fafbfb;
}
.list-section-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin: 18px 0 9px;
  padding-bottom: 9px;
  border-bottom: 1px solid #e6eaed;
}
.list-section-head h3 {
  margin: 0;
  color: #27323b;
  font-size: 15px;
}
.list-section-head p,
.list-section-head > span {
  margin: 3px 0 0;
  color: #68727f;
  font-size: 12px;
}
.ticket-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 10px;
  margin-top: 5px;
}
.ticket-meta__issue {
  color: #46534d;
  font-size: 12px;
}
.assign-recommendations {
  width: 100%;
  display: grid;
  gap: 8px;
}
.assign-candidate {
  width: 100%;
  border: 1px solid var(--pic-line, #e1e5e9);
  border-radius: 6px;
  padding: 9px 10px;
  background: #fff;
  color: inherit;
  text-align: left;
  cursor: pointer;
  display: grid;
  gap: 3px;
}
.assign-candidate:hover,
.assign-candidate--selected {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.assign-candidate__head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.assign-candidate__load,
.assign-candidate__reason {
  font-size: 12px;
  line-height: 1.5;
  color: #68727f;
}
.tk__pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
}
.cell-strong {
  font-weight: 600;
}
.cell-sub {
  font-size: 12px;
  color: #5f6872;
}
.prio {
  font-size: 12px;
}
.prio--urgent,
.prio--high {
  color: #b44141;
}
.prio--medium {
  color: #ad6017;
}
.prio--low {
  color: #68727f;
}
.pill {
  display: inline-block;
  padding: 3px 8px;
  border-radius: 20px;
  font-size: 11px;
  line-height: 1.5;
}
.pill--todo {
  background: #eaf2f9;
  color: #396d9d;
}
.pill--doing {
  background: #e5f3ef;
  color: #187c67;
}
.pill--warn {
  background: #fff2df;
  color: #ad6017;
}
.pill--done {
  background: #e5f3ef;
  color: #187c67;
}
.pill--risk {
  background: #faeaea;
  color: #b44141;
}
.drawer-head {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
}
.drawer-body {
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(0, 0.92fr);
  gap: 14px;
  min-width: 0;
}
.col {
  display: grid;
  gap: 14px;
  align-content: start;
  min-width: 0;
}
.col--full {
  grid-column: 1 / -1;
}
.next-step-banner {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: 4px 14px;
  padding: 13px 16px;
  border: 1px solid #bad8ce;
  border-left: 4px solid #187c67;
  border-radius: 6px;
  background: #f1f8f5;
}
.next-step-banner__label {
  grid-row: span 2;
  color: #187c67;
  font-size: 12px;
  font-weight: 700;
}
.next-step-banner strong {
  color: #273b34;
  font-size: 14px;
  line-height: 1.5;
}
.next-step-banner__guard {
  color: #9a591b;
  font-size: 12px;
}
.section-number {
  display: inline-flex;
  width: 24px;
  height: 24px;
  align-items: center;
  justify-content: center;
  margin-right: 8px;
  border-radius: 4px;
  background: #eaf3ef;
  color: #187c67;
  font-size: 11px;
  vertical-align: 1px;
}
.card__intro {
  margin: -2px 0 10px;
  color: #68727f;
  font-size: 12px;
  line-height: 1.5;
}
.handling-records__grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 0.9fr);
  gap: 20px;
  margin-top: 10px;
}
.handling-records__block {
  min-width: 0;
}
.handling-records__block + .handling-records__block {
  padding-left: 18px;
  border-left: 1px solid #eef1f3;
}
.handling-records__block h4 {
  margin-top: 0;
}
.card {
  box-sizing: border-box;
  min-width: 0;
  border: 1px solid var(--pic-line, #e1e5e9);
  border-radius: 6px;
  padding: 14px 16px;
  background: #fff;
}
.card--audit {
  background: #fafbfb;
}
.card__title {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.card__title h3,
.card h3 {
  margin: 0 0 8px;
  font-size: 15px;
}
.card h4 {
  margin: 14px 0 6px;
  font-size: 13px;
  color: #37474f;
}
.quote {
  background: #f4f6f7;
  border-radius: 4px;
  padding: 10px 12px;
  line-height: 1.6;
}
.kv {
  margin: 0;
  display: grid;
  grid-template-columns: 100px minmax(0, 1fr);
  row-gap: 6px;
}
.kv dt {
  color: #5f6872;
  font-size: 12px;
}
.kv dd {
  min-width: 0;
  margin: 0;
  font-size: 13px;
  overflow-wrap: anywhere;
}
.tag-mini {
  margin-left: 6px;
  font-size: 11px;
  color: #187c67;
  background: #e5f3ef;
  border-radius: 3px;
  padding: 0 4px;
}
.tag-mini--warn {
  color: #ad6017;
  background: #fff2df;
}
.agent-line {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 0;
  border-bottom: 1px dashed #eef1f3;
}
.agent-line__icon {
  width: 24px;
  height: 24px;
  border-radius: 4px;
  background: #eaf2f9;
  color: #396d9d;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
}
.agent-line__body {
  flex: 1;
}
.ok {
  color: #187c67;
  font-size: 12px;
}
.fail {
  color: #b44141;
  font-size: 12px;
}
.run {
  color: #ad6017;
  font-size: 12px;
}
.action {
  font-size: 14px;
  margin-bottom: 4px;
}
.evidence {
  border-left: 2px solid #9abeb4;
  background: #f5f9f8;
  padding: 8px 10px;
  border-radius: 0 4px 4px 0;
  margin: 8px 0;
}
.evidence__title {
  font-size: 12px;
  color: #187c67;
  font-weight: 600;
}
.evidence__quote {
  font-size: 12px;
  color: #4c5b5b;
  line-height: 1.6;
  margin-top: 4px;
}
.risk-box {
  background: #fff2df;
  border: 1px solid #f0d5b2;
  color: #754618;
  border-radius: 4px;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.7;
  margin: 10px 0;
}
.risk-box__warn {
  color: #9a591b;
}
.card__actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 10px;
}
.log {
  background: #f8f9fa;
  border-radius: 4px;
  padding: 8px 10px;
  max-height: 160px;
  overflow: auto;
  font-size: 12px;
}
.log__line + .log__line {
  margin-top: 4px;
}
.policy-line {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 0;
  font-size: 13px;
}
.toast {
  position: fixed;
  left: 50%;
  bottom: 24px;
  transform: translateX(-50%);
  background: #1b252d;
  color: #fff;
  padding: 9px 16px;
  border-radius: 6px;
  font-size: 13px;
  z-index: 3000;
  box-shadow: 0 8px 24px rgba(28, 39, 48, 0.24);
}
@media (max-width: 900px) {
  .tk__stats {
    grid-template-columns: repeat(2, 1fr);
  }
  .drawer-body {
    grid-template-columns: minmax(0, 1fr);
  }
  .advanced-filters {
    width: 100%;
  }
  .advanced-filters__body {
    width: 100%;
  }
  .handling-records__grid {
    grid-template-columns: 1fr;
  }
  .handling-records__block + .handling-records__block {
    padding: 14px 0 0;
    border-top: 1px solid #eef1f3;
    border-left: 0;
  }
  .template-manager { grid-template-columns: 1fr; }
}
@media (max-width: 520px) {
  :global(.ticket-detail-drawer.el-drawer) {
    width: 100vw !important;
    max-width: 100vw;
    min-width: 0;
  }
  :global(.ticket-detail-drawer .el-drawer__header),
  :global(.ticket-detail-drawer .el-drawer__body) {
    padding-right: 14px;
    padding-left: 14px;
  }
  .drawer-head {
    flex-wrap: wrap;
  }
  .drawer-head > span[style] {
    display: none;
  }
  .next-step-banner {
    grid-template-columns: 1fr;
  }
  .next-step-banner__label {
    grid-row: auto;
  }
  .card__title {
    align-items: flex-start;
  }
  .handling-records .card__title {
    flex-direction: column;
    align-items: flex-start;
  }
  .note-actions {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>

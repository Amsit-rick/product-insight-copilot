<script setup lang="ts">
/**
 * 系统状态：把"只能看接口"的运行时状态搬到界面上，让人工测试不用敲命令。
 * 覆盖：健康检查、模型路由/灰度、限额与预算、缓存、成本、任务执行后端、
 * 队列积压、断点恢复是否跨进程共享。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { operationalAlertApi, queueApi, systemApi } from '@/api/endpoints'
import type {
  CanaryExperiment,
  CostSummary,
  DeliveryReadinessSnapshot,
  OperationalAlert,
  OperationalAlertDeliveryList,
  OperationalAlertList,
  ProactiveTriggerStatus,
  QueueItem,
  QueueStats,
  ReflectionReleaseReport,
} from '@/api/types'
import { useSessionStore } from '@/stores/session'

type Health = {
  status: string
  app?: string
  env?: string
  database: string
  vector_backend: string
  workflow_version?: string
  prompt_version?: string
  llm?: {
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
  embedding?: { provider: string; enabled: boolean; model: string; dim: number }
  reflection?: {
    enabled: boolean
    ticket_enabled: boolean
    report_enabled: boolean
    rollout_percent: number
    rollout_tenants: string[]
    rollout_risk_levels: string[]
    rollout_issue_types: string[]
    release_min_sample: number
    auto_guard: boolean
  }
  limits?: Record<string, unknown>
  cache?: {
    backend: string
    configured?: string
    healthy?: boolean
    degraded_reason?: string
    persistent?: boolean
    entries?: number
    path?: string
    hits: number
    misses: number
    hit_rate: number
    // 按命名空间细分：一个总命中率说明不了问题（冷启动的检索必然接近 0%）
    namespaces?: Record<string, { hits: number; misses: number; total_lookups: number; hit_rate: number }>
    recent?: { window: number; hits: number; hit_rate: number }
    vector_cache?: {
      enabled: boolean
      entries: number
      max_entries: number
      hits: number
      misses: number
      hit_rate: number
    }
  }
  cost?: { currency: string; priced_models: string[] }
  execution?: {
    backend: string
    configured_backend: string
    worker_embedded: boolean
    worker_concurrency: number
    checkpointer: { backend: string; shared: boolean; note?: string; path?: string }
    queue: { pending: number; claimed: number; done: number; dead: number; oldest_pending_at?: string | null }
  }
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
}

const loading = ref(false)
const router = useRouter()
const health = ref<Health | null>(null)
const costs = ref<CostSummary | null>(null)
const canary = ref<CanaryExperiment | null>(null)
const reflectionRelease = ref<ReflectionReleaseReport | null>(null)
const deliveryReadiness = ref<DeliveryReadinessSnapshot | null>(null)
const deliveryLoading = ref(false)
const deliveryPanelOpen = ref(false)
const releaseAction = ref('')
const session = useSessionStore()

// ---- 队列运维（死信排查与重放，仅管理员）----
const queueStats = ref<QueueStats | null>(null)
const deadItems = ref<QueueItem[]>([])
const queueLoading = ref(false)
const replaying = ref('')
const canAdmin = computed(() => session.can('admin:manage'))

// ---- 运行告警（自动回滚等需要管理员处理的事件）----
const operationalAlerts = ref<OperationalAlertList | null>(null)
const alertDeliveries = ref<OperationalAlertDeliveryList | null>(null)
const alertLoading = ref(false)
const alertAction = ref('')
const proactiveTrigger = ref<ProactiveTriggerStatus | null>(null)

async function loadOperationalAlerts(): Promise<void> {
  if (!canAdmin.value) return
  alertLoading.value = true
  try {
    const [alerts, deliveries, trigger] = await Promise.all([
      operationalAlertApi.list({ limit: 50 }),
      operationalAlertApi.deliveries({ status: 'dead', limit: 50 }),
      operationalAlertApi.proactiveStatus(),
    ])
    operationalAlerts.value = alerts
    alertDeliveries.value = deliveries
    proactiveTrigger.value = trigger
  } catch (error) {
    operationalAlerts.value = null
    alertDeliveries.value = null
    proactiveTrigger.value = null
    ElMessage.warning(`运行告警加载失败：${(error as Error).message}`)
  } finally {
    alertLoading.value = false
  }
}

async function scanProactiveAnomalies(): Promise<void> {
  try {
    alertAction.value = 'proactive-scan'
    const result = await operationalAlertApi.scanProactive()
    ElMessage.success(
      result.anomalies_found > 0
        ? `只读扫描完成，发现 ${result.anomalies_found} 个候选异常`
        : result.skipped.length > 0
          ? `扫描未产生候选，${result.skipped.length} 个指标因数据新鲜度或权限门禁被跳过`
        : '只读扫描完成，未发现候选异常',
    )
    await loadOperationalAlerts()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    alertAction.value = ''
  }
}

function diagnosisTaskId(alert: OperationalAlert): string {
  const value = alert.metadata?.diagnosis_task_id
  return typeof value === 'string' ? value : ''
}

async function createProactiveDiagnosis(alert: OperationalAlert): Promise<void> {
  const existing = diagnosisTaskId(alert)
  if (existing) {
    void router.push({ name: 'task-progress', params: { id: existing } })
    return
  }
  try {
    await ElMessageBox.confirm(
      '确认后才会创建并启动多 Agent 诊断。只读扫描本身没有调用模型或执行外部动作。',
      '创建诊断任务',
      { type: 'warning' },
    )
    alertAction.value = `diagnose:${alert.id}`
    const result = await operationalAlertApi.createDiagnosis(alert.id)
    ElMessage.success(result.created ? '诊断任务已创建' : '该候选已存在诊断任务')
    void router.push({ name: 'task-progress', params: { id: result.task.id } })
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error((error as Error).message)
  } finally {
    alertAction.value = ''
  }
}

async function replayAlertDelivery(deliveryId: string): Promise<void> {
  try {
    await ElMessageBox.confirm('重新投递这条 Webhook 死信？操作会记录审计日志。', '重放外部通知', {
      type: 'warning',
    })
    alertAction.value = `replay:${deliveryId}`
    await operationalAlertApi.replayDelivery(deliveryId)
    ElMessage.success('已放回外部通知队列')
    await loadOperationalAlerts()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error((error as Error).message)
  } finally {
    alertAction.value = ''
  }
}

async function updateOperationalAlert(
  alert: OperationalAlert,
  action: 'acknowledge' | 'resolve',
): Promise<void> {
  const verb = action === 'acknowledge' ? '确认已看到' : '标记为已解决'
  try {
    await ElMessageBox.confirm(`${verb}“${alert.title}”？操作会记录审计日志。`, '处理运行告警', {
      type: action === 'resolve' ? 'warning' : 'info',
    })
    alertAction.value = `${action}:${alert.id}`
    if (action === 'acknowledge') await operationalAlertApi.acknowledge(alert.id)
    else await operationalAlertApi.resolve(alert.id)
    ElMessage.success(`已${verb}`)
    await loadOperationalAlerts()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error((error as Error).message)
  } finally {
    alertAction.value = ''
  }
}

function alertSeverityType(severity: string): 'danger' | 'warning' | 'info' {
  if (severity === 'critical') return 'danger'
  if (severity === 'warning') return 'warning'
  return 'info'
}

function alertStatusText(status: string): string {
  return { open: '待处理', acknowledged: '已确认', resolved: '已解决' }[status] || status
}

async function loadQueue(): Promise<void> {
  if (!canAdmin.value) return
  queueLoading.value = true
  try {
    const data = await queueApi.items({ status: 'dead', limit: 50 })
    queueStats.value = data.stats
    deadItems.value = data.items
  } catch (error) {
    queueStats.value = null
    deadItems.value = []
    ElMessage.warning(`队列信息加载失败：${(error as Error).message}`)
  } finally {
    queueLoading.value = false
  }
}

async function replayOne(item: QueueItem): Promise<void> {
  replaying.value = item.id
  try {
    await queueApi.replay(item.id)
    ElMessage.success('已放回队列，worker 会重新领取')
    await loadQueue()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    replaying.value = ''
  }
}

async function replayAll(): Promise<void> {
  replaying.value = 'all'
  try {
    const result = await queueApi.replayDead()
    ElMessage.success(`已放回 ${result.replayed} 条死信`)
    await loadQueue()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    replaying.value = ''
  }
}

/** 等待时长：pending 最老那条已经等了多久（队列堵没堵一眼就能看出来） */
const oldestPendingText = computed(() => {
  const raw = queueStats.value?.oldest_pending_at
  if (!raw) return '—'
  const seconds = Math.max(0, Math.round((Date.now() - new Date(raw).getTime()) / 1000))
  return seconds < 60 ? `${seconds} 秒` : `${Math.round(seconds / 60)} 分钟`
})

const limitRows = computed(() => {
  const limits = (health.value?.limits ?? {}) as Record<string, unknown>
  const label: Record<string, string> = {
    model_max_concurrency: '模型并发上限',
    task_submit_rate_limit_per_minute: '建任务限流（每分钟）',
    task_token_limit: '单任务 token 上限（0=不限）',
    task_cost_limit: '单任务成本上限（0=不限）',
    tenant_daily_cost_limit: '租户日成本上限（0=不限）',
    budget_exceeded_action: '超限动作',
    retrieval_cache_ttl_seconds: '检索缓存 TTL（秒）',
    metric_cache_ttl_seconds: '指标缓存 TTL（秒）',
    fallback_models: '模型降级链',
  }
  return Object.entries(label)
    .filter(([key]) => key in limits)
    .map(([key, text]) => ({ key, label: text, value: JSON.stringify(limits[key]) }))
})

const canaryRows = computed(() =>
  (canary.value ? [...canary.value.metrics, ...canary.value.report_metrics] : []).map((item) => ({
    ...item,
    deltaText:
      item.test === 'two-proportion-z'
        ? `${(item.delta_pp ?? 0).toFixed(1)}pp`
        : `${item.delta_pct.toFixed(1)}%`,
  })),
)

const releaseTagType = computed(() => {
  const recommendation = reflectionRelease.value?.decision.recommendation
  if (recommendation === 'rollback') return 'danger'
  if (recommendation === 'pause') return 'warning'
  if (recommendation === 'promote') return 'success'
  return 'info'
})

const deliveryStatus = computed(() => {
  const status = deliveryReadiness.value?.status
  const labels: Record<string, { text: string; type: 'success' | 'warning' | 'danger' | 'info' }> = {
    current: { text: '证据有效', type: 'success' },
    stale: { text: '证据已过期', type: 'warning' },
    version_unbound: { text: '证据未绑定当前发布版本', type: 'warning' },
    commit_mismatch: { text: '证据与当前发布版本不一致', type: 'danger' },
    incomplete: { text: '证据内容不完整', type: 'danger' },
    invalid: { text: '证据无效', type: 'danger' },
    missing: { text: '尚未生成证据', type: 'info' },
  }
  return labels[status || 'missing'] || { text: status || '未知', type: 'info' as const }
})

const deliveryReasonText = computed(() => {
  const labels: Record<string, string> = {
    release_report_missing: '预发布报告缺失',
    delivery_check_missing: '交付检查缺失',
    evidence_manifest_missing: '交付证据 manifest 缺失',
    evidence_bundle_hash_mismatch: '证据文件哈希与 manifest 不一致',
    evidence_bundle_generation_mismatch: '报告与交付检查不属于同一证据批次',
    evidence_bundle_release_mismatch: '证据包发布版本与预发布报告不一致',
    evidence_expired: '证据超过允许有效期',
    runtime_release_sha_missing: '运行版本未注入 APP_RELEASE_SHA，无法核对提交',
    release_commit_mismatch: '证据提交与当前运行版本不一致',
    external_gate_missing: '六类外部门禁未完整写入报告',
    schema_invalid: '证据格式版本不符合当前契约',
    generated_at_invalid: '证据时间戳缺失或无时区',
    generated_at_in_future: '证据时间戳异常地位于未来',
  }
  return (deliveryReadiness.value?.reasons || []).map((item) => labels[item] || item).join('；')
})

function deliveryGateType(status: string): 'success' | 'danger' | 'warning' {
  if (status === 'verified_external') return 'success'
  if (status === 'blocked_external') return 'danger'
  return 'warning'
}

function deliveryGateText(status: string): string {
  if (status === 'verified_external') return '已验证'
  if (status === 'blocked_external') return '待外部验证'
  return '未写入证据'
}

async function loadDeliveryReadiness(): Promise<void> {
  if (!canAdmin.value) return
  deliveryLoading.value = true
  try {
    deliveryReadiness.value = await systemApi.deliveryReadiness()
  } catch (error) {
    deliveryReadiness.value = null
    ElMessage.warning(`正式交付证据加载失败：${(error as Error).message}`)
  } finally {
    deliveryLoading.value = false
  }
}

async function openDeliveryReadiness(): Promise<void> {
  deliveryPanelOpen.value = true
  if (!deliveryReadiness.value) await loadDeliveryReadiness()
}

function scopeText(values: string[]): string {
  return values.length ? values.join('、') : '全部'
}

async function changeReflectionRelease(
  action: 'evaluate' | 'approve_promotion' | 'pause' | 'rollback' | 'resume',
  percent?: number,
): Promise<void> {
  const labels: Record<string, string> = {
    evaluate: '重新计算发布建议',
    approve_promotion: `批准扩大到 ${percent}%`,
    pause: '暂停反思灰度',
    rollback: '回滚反思灰度到 0%',
    resume: '恢复反思灰度',
  }
  try {
    await ElMessageBox.confirm(
      `${labels[action]}？所有操作都会记录审计日志。`,
      '确认发布操作',
      { type: action === 'rollback' ? 'warning' : 'info' },
    )
    releaseAction.value = action
    reflectionRelease.value = await systemApi.reflectionReleaseDecision({
      action,
      percent,
      reason: labels[action],
    })
    ElMessage.success('发布状态已更新')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error((error as Error).message)
  } finally {
    releaseAction.value = ''
  }
}

/** 缓存分层说明：把"命中率低"翻译成"低在哪、正不正常" */
const CACHE_LAYER_NOTES: Record<string, string> = {
  retrieval: '每个 Agent 问的都是新问题 → 冷启动天然低；同题重跑才命中',
  metric: '同一任务里多个 Agent 查同一指标 → 复用率高',
  embedding: '同一段查询词被多个 Agent 重复使用 → 省掉重复向量化（接了真实模型就是省钱）',
}

const cacheNamespaceRows = computed(() => {
  const buckets = health.value?.cache?.namespaces ?? {}
  return Object.entries(buckets)
    .sort((a, b) => b[1].total_lookups - a[1].total_lookups)
    .map(([name, bucket]) => ({
      name,
      label: name,
      hits: bucket.hits,
      lookups: bucket.total_lookups,
      rate: `${(bucket.hit_rate * 100).toFixed(0)}%`,
      note: CACHE_LAYER_NOTES[name] || '—',
    }))
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const [h, c, release] = await Promise.all([
      systemApi.health(),
      systemApi.costs(7),
      systemApi.reflectionRelease(14),
    ])
    health.value = h as unknown as Health
    costs.value = c
    reflectionRelease.value = release
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
  try {
    canary.value = await systemApi.canaryExperiment(14)
  } catch {
    canary.value = null
  }
  // 队列运维与主信息分开 try：这一项失败（例如非管理员）不该影响页面其余部分
  await Promise.all([loadQueue(), loadOperationalAlerts()])
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <el-card shadow="never">
      <template #header>
        <span style="font-weight: 600">系统状态</span>
        <el-tag v-if="health" :type="health.status === 'ok' ? 'success' : 'danger'" size="small" style="margin-left: 10px">
          {{ health.status }}
        </el-tag>
        <div style="float: right; display: flex; gap: 4px">
          <el-button
            v-if="canAdmin"
            data-testid="delivery-readiness-trigger"
            link
            type="primary"
            @click="openDeliveryReadiness"
          >
            交付验收
          </el-button>
          <el-button link type="primary" @click="load">刷新</el-button>
        </div>
      </template>

      <el-row :gutter="16">
        <el-col :span="8">
          <div class="pic-muted">运行环境</div>
          <div>{{ health?.env }} · {{ health?.database }} · 向量 {{ health?.vector_backend }}</div>
        </el-col>
        <el-col :span="8">
          <div class="pic-muted">版本</div>
          <div>工作流 {{ health?.workflow_version }} · Prompt {{ health?.prompt_version }}</div>
        </el-col>
        <el-col :span="8">
          <div class="pic-muted">模型网关</div>
          <div>
            <el-tag :type="health?.llm?.enabled ? 'success' : 'info'" size="small">
              {{ health?.llm?.enabled ? health?.llm?.model : '未启用（stub）' }}
            </el-tag>
            <span class="pic-muted" style="margin-left: 8px">{{ health?.llm?.provider }}</span>
          </div>
          <div v-if="health?.llm?.runtime_metrics" class="pic-muted" style="margin-top: 4px">
            JSON 失败 {{ health.llm.runtime_metrics.json_parse_failures }} / 调用
            {{ health.llm.runtime_metrics.chat_json_calls }} / 回退
            {{ health.llm.runtime_metrics.fallbacks }}
          </div>
        </el-col>
      </el-row>
    </el-card>

    <el-drawer
      v-if="canAdmin"
      v-model="deliveryPanelOpen"
      title="交付验收详情"
      size="min(760px, 92vw)"
      destroy-on-close
    >
      <el-card shadow="never" v-loading="deliveryLoading">
        <template #header>
          <span style="font-weight: 600">正式交付证据</span>
          <el-tag :type="deliveryStatus.type" size="small" style="margin-left: 10px">
            {{ deliveryStatus.text }}
          </el-tag>
          <span class="pic-muted" style="margin-left: 10px">只读证据快照，不等同于当前进程健康状态</span>
          <el-button link type="primary" style="float: right" @click="loadDeliveryReadiness">刷新</el-button>
        </template>

      <el-alert
        v-if="!deliveryReadiness?.trusted_production_ready"
        type="warning"
        :closable="false"
        show-icon
        title="当前证据不能作为生产发布依据"
        :description="deliveryReasonText || '仍有机器门禁、外部门禁或人工审批未完成。'"
      />
      <el-alert
        v-else
        type="success"
        :closable="false"
        show-icon
        title="当前版本的正式证据与发布批准均有效"
      />

      <el-descriptions :column="4" border size="small" style="margin-top: 12px">
        <el-descriptions-item label="本地演示">
          <el-tag size="small" :type="deliveryReadiness?.local_demo_ready ? 'success' : 'danger'">
            {{ deliveryReadiness?.local_demo_ready ? '可演示' : '未就绪' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="发布候选">
          <el-tag size="small" :type="deliveryReadiness?.candidate_ready ? 'success' : 'danger'">
            {{ deliveryReadiness?.candidate_ready ? '可交付' : '不可交付' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="生产验收">
          <el-tag size="small" :type="deliveryReadiness?.trusted_production_ready ? 'success' : 'danger'">
            {{ deliveryReadiness?.trusted_production_ready ? '已验收' : '未验收' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="证据快照">
          <template v-if="deliveryReadiness?.generated_at">
            <div>{{ new Date(deliveryReadiness.generated_at).toLocaleString() }}</div>
            <span class="pic-muted">提交 {{ deliveryReadiness.evidence_commit_sha || '未记录' }}</span>
            <el-tag
              size="small"
              :type="deliveryReadiness.evidence_bundle_verified ? 'success' : 'danger'"
              style="margin-left: 8px"
            >
              {{ deliveryReadiness.evidence_bundle_verified ? '证据包一致' : '证据包未校验' }}
            </el-tag>
          </template>
          <template v-else>尚未生成</template>
        </el-descriptions-item>
      </el-descriptions>

      <el-table :data="deliveryReadiness?.external_gates || []" size="small" style="margin-top: 12px">
        <el-table-column prop="label" label="生产交付门禁" min-width="240" />
        <el-table-column label="状态" width="140">
          <template #default="{ row }">
            <el-tag size="small" :type="deliveryGateType(row.status)">
              {{ deliveryGateText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="required" label="完成要求" min-width="360">
          <template #default="{ row }">{{ row.required || '重新生成完整预发布报告' }}</template>
        </el-table-column>
      </el-table>

      <el-collapse v-if="deliveryReadiness?.release_checks.length" style="margin-top: 12px">
        <el-collapse-item title="查看机器门禁明细" name="release-checks">
          <el-table :data="deliveryReadiness.release_checks" size="small">
            <el-table-column prop="name" label="检查" min-width="180" />
            <el-table-column label="结果" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="row.status === 'PASS' ? 'success' : 'danger'">
                  {{ row.status === 'PASS' ? '通过' : '未通过' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="detail" label="说明" min-width="360" />
          </el-table>
        </el-collapse-item>
      </el-collapse>
      </el-card>
    </el-drawer>

    <el-card v-if="canAdmin" shadow="never" class="pic-section-gap" v-loading="alertLoading">
      <template #header>
        <span style="font-weight: 600">运行告警与主动发现</span>
        <el-tag
          size="small"
          :type="(operationalAlerts?.summary.critical ?? 0) > 0 ? 'danger' : 'success'"
          style="margin-left: 10px"
        >
          严重待处理 {{ operationalAlerts?.summary.critical ?? 0 }}
        </el-tag>
        <span class="pic-muted" style="margin-left: 10px">自动回滚等关键事件会在这里持续保留，直到人工解决</span>
        <el-button link type="primary" style="float: right" @click="loadOperationalAlerts">刷新</el-button>
      </template>

      <section data-testid="proactive-anomaly-trigger" class="proactive-trigger">
        <div>
          <div class="proactive-trigger__title">
            <span>只读主动异常触发器</span>
            <el-tag size="small" :type="proactiveTrigger?.enabled ? 'success' : 'info'">
              {{ proactiveTrigger?.enabled ? '已启用' : '默认关闭' }}
            </el-tag>
          </div>
          <p>
            仅扫描已授权指标并生成候选异常，不调用模型、不自动启动 Agent，也不执行退款、改价或消息发送。
            管理员确认后才会创建诊断任务。
          </p>
          <div class="pic-muted">
            指标 {{ proactiveTrigger?.metrics.join('、') || '未配置' }} · 最近
            {{ proactiveTrigger?.lookback_days ?? '—' }} 天 · 阈值
            {{ proactiveTrigger?.threshold ?? '—' }} · 数据最大延迟
            {{ proactiveTrigger?.max_data_age_days ?? '—' }} 天
          </div>
          <div class="pic-muted">
            指标事实源 ·
            <template v-if="proactiveTrigger?.metric_source.enabled">
              {{ proactiveTrigger.metric_source.configured ? '已配置（上游只读）' : '配置无效' }}
            </template>
            <template v-else>默认关闭</template>
            · 分指标新鲜度策略
            {{ Object.keys(proactiveTrigger?.max_data_age_by_metric || {}).length }} 项
          </div>
          <div class="pic-muted">
            独立调度进程 ·
            <template v-if="proactiveTrigger?.scheduler">
              {{ proactiveTrigger.scheduler.running ? '正在扫描' : '当前空闲' }} · 最近结果
              {{ proactiveTrigger.scheduler.last_status || '尚未完成' }} · 异常候选
              {{ proactiveTrigger.scheduler.last_result.anomalies_found ?? 0 }} 个
              · 数据门禁跳过 {{ proactiveTrigger.scheduler.last_result.metrics_skipped ?? 0 }} 个
              · 指标源写入 {{ proactiveTrigger.scheduler.last_result.source_rows_written ?? 0 }} 行
              <template v-if="proactiveTrigger.scheduler.last_run_finished_at">
                · {{ new Date(proactiveTrigger.scheduler.last_run_finished_at).toLocaleString() }}
              </template>
            </template>
            <template v-else>尚未运行</template>
          </div>
        </div>
        <el-button
          type="primary"
          plain
          :disabled="!proactiveTrigger?.enabled"
          :loading="alertAction === 'proactive-scan'"
          @click="scanProactiveAnomalies"
        >
          立即只读扫描
        </el-button>
      </section>

      <el-descriptions :column="4" border size="small">
        <el-descriptions-item label="待处理">{{ operationalAlerts?.summary.open ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="已确认、处理中">{{ operationalAlerts?.summary.acknowledged ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="严重未解决">{{ operationalAlerts?.summary.critical ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="站外 Webhook">
          <el-tag
            size="small"
            :type="health?.alerting?.webhook.enabled ? 'success' : 'info'"
          >
            {{ health?.alerting?.webhook.enabled ? '已启用' : '默认关闭' }}
          </el-tag>
          <span v-if="health?.alerting?.webhook.enabled" class="pic-muted" style="margin-left: 6px">
            {{ health?.alerting?.webhook.signed ? '已签名' : '未配置签名' }}
          </span>
        </el-descriptions-item>
      </el-descriptions>

      <el-alert
        v-if="!(operationalAlerts?.items.length)"
        type="success"
        :closable="false"
        style="margin-top: 12px"
        title="当前没有运行告警"
        description="这里显示的是需要管理员处理的事件；普通审计记录仍保留在审计日志中。"
      />
      <el-table v-else :data="operationalAlerts?.items" size="small" style="margin-top: 12px">
        <el-table-column label="告警" min-width="300">
          <template #default="{ row }">
            <div>
              <el-tag size="small" :type="alertSeverityType(row.severity)">{{ row.severity }}</el-tag>
              <b style="margin-left: 8px">{{ row.title }}</b>
            </div>
            <div style="margin-top: 5px">{{ row.message }}</div>
            <div class="pic-muted" style="margin-top: 3px">
              来源 {{ row.source_type || '系统' }} · 最近发生 {{ new Date(row.last_occurred_at).toLocaleString() }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'resolved' ? 'success' : row.status === 'open' ? 'danger' : 'warning'">
              {{ alertStatusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="发生次数" width="100">
          <template #default="{ row }">{{ row.occurrence_count }} 次</template>
        </el-table-column>
        <el-table-column label="操作" width="190">
          <template #default="{ row }">
            <el-button
              v-if="row.kind === 'proactive_metric_anomaly'"
              link
              type="primary"
              :loading="alertAction === `diagnose:${row.id}`"
              @click="createProactiveDiagnosis(row)"
            >
              {{ diagnosisTaskId(row) ? '查看诊断任务' : '创建诊断任务' }}
            </el-button>
            <el-button
              v-if="row.status === 'open'"
              link
              type="primary"
              :loading="alertAction === `acknowledge:${row.id}`"
              @click="updateOperationalAlert(row, 'acknowledge')"
            >
              确认已看到
            </el-button>
            <el-button
              v-if="row.status !== 'resolved'"
              link
              type="success"
              :loading="alertAction === `resolve:${row.id}`"
              @click="updateOperationalAlert(row, 'resolve')"
            >
              标记已解决
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-alert
        v-if="(health?.alerting?.delivery_queue.dead ?? 0) > 0"
        type="error"
        :closable="false"
        style="margin-top: 12px"
        :title="`有 ${health?.alerting?.delivery_queue.dead ?? 0} 条外部通知投递失败`"
        description="达到最大重试次数或收到不可重试的 4xx 后会进入死信；请先确认接收端配置，再重放。"
      />
      <el-table
        v-if="alertDeliveries?.items.length"
        :data="alertDeliveries.items"
        size="small"
        style="margin-top: 12px"
      >
        <el-table-column label="Webhook 死信" min-width="260">
          <template #default="{ row }">
            <div>{{ row.last_error || '未记录错误' }}</div>
            <div class="pic-muted">
              告警 {{ row.alert_id.slice(0, 8) }}… · 第 {{ row.occurrence_no }} 次发生
            </div>
          </template>
        </el-table-column>
        <el-table-column label="尝试" width="100">
          <template #default="{ row }">{{ row.attempts }}/{{ row.max_attempts }}</template>
        </el-table-column>
        <el-table-column label="HTTP" width="90">
          <template #default="{ row }">{{ row.last_status_code ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              :loading="alertAction === `replay:${row.id}`"
              @click="replayAlertDelivery(row.id)"
            >
              重新投递
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" class="pic-section-gap">
      <template #header><span style="font-weight: 600">任务执行与断点恢复（决定能不能多副本）</span></template>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="执行后端">
          <el-tag size="small" :type="health?.execution?.backend === 'queue' ? 'success' : 'warning'">
            {{ health?.execution?.backend }}
          </el-tag>
          <span class="pic-muted" style="margin-left: 8px">配置值 {{ health?.execution?.configured_backend }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="内嵌 worker">
          {{ health?.execution?.worker_embedded ? '是（与 API 同进程）' : '否（独立 worker 进程）' }}
        </el-descriptions-item>
        <el-descriptions-item label="断点恢复（checkpointer）">
          <el-tag size="small" :type="health?.execution?.checkpointer.shared ? 'success' : 'warning'">
            {{ health?.execution?.checkpointer.backend }}
          </el-tag>
          <span class="pic-muted" style="margin-left: 8px">
            {{ health?.execution?.checkpointer.shared ? '可跨进程/副本共享' : '进程内内存，重启或换副本会降级恢复' }}
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="队列积压">
          待处理 {{ health?.execution?.queue.pending ?? 0 }} · 执行中 {{ health?.execution?.queue.claimed ?? 0 }} ·
          已完成 {{ health?.execution?.queue.done ?? 0 }} · 判死 {{ health?.execution?.queue.dead ?? 0 }}
        </el-descriptions-item>
      </el-descriptions>
      <div v-if="health?.execution?.checkpointer.note" class="pic-muted" style="margin-top: 8px">
        {{ health?.execution?.checkpointer.note }}
      </div>
    </el-card>

    <el-card v-if="canAdmin" shadow="never" class="pic-section-gap" v-loading="queueLoading">
      <template #header>
        <span style="font-weight: 600">队列运维（死信排查与重放）</span>
        <span class="pic-muted" style="margin-left: 10px">
          只有 dead 能重放；重放会清零尝试次数并保留"上次为什么死"
        </span>
        <el-button
          type="primary"
          size="small"
          style="float: right"
          :disabled="!deadItems.length"
          :loading="replaying === 'all'"
          @click="replayAll"
        >
          全部放回队列
        </el-button>
        <el-button size="small" style="float: right; margin-right: 8px" @click="loadQueue">刷新</el-button>
      </template>

      <el-descriptions :column="4" border size="small">
        <el-descriptions-item label="待处理">{{ queueStats?.pending ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="执行中">{{ queueStats?.claimed ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="已完成">{{ queueStats?.done ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="判死（死信）">
          <el-tag size="small" :type="(queueStats?.dead ?? 0) > 0 ? 'danger' : 'success'">
            {{ queueStats?.dead ?? 0 }}
          </el-tag>
          <span class="pic-muted" style="margin-left: 8px">最老待处理已等 {{ oldestPendingText }}</span>
        </el-descriptions-item>
      </el-descriptions>

      <el-alert
        v-if="!deadItems.length"
        type="success"
        :closable="false"
        style="margin-top: 12px"
        title="没有死信：队列干净"
        description="死信来自「重试次数耗尽」（执行反复失败）或「worker 反复崩溃导致租约回收耗尽」。出现死信时这里会列出是哪些任务、错误是什么。"
      />
      <el-table v-else :data="deadItems" size="small" style="margin-top: 12px">
        <el-table-column label="任务" min-width="240">
          <template #default="{ row }">
            <div>{{ row.task_query || '（任务记录已不存在）' }}</div>
            <div class="pic-muted">
              {{ row.task_id.slice(0, 8) }}… · 任务状态 {{ row.task_status || '—' }} · 类型 {{ row.kind }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="尝试" width="90">
          <template #default="{ row }">{{ row.attempts }}/{{ row.max_attempts }}</template>
        </el-table-column>
        <el-table-column label="最后错误" min-width="240">
          <template #default="{ row }">
            <span class="pic-muted">{{ (row.last_error || '—').slice(0, 120) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="重放过" width="90">
          <template #default="{ row }">{{ row.replays }} 次</template>
        </el-table-column>
        <el-table-column label="判死时间" width="170">
          <template #default="{ row }">
            <span class="pic-muted">{{ row.finished_at ? new Date(row.finished_at).toLocaleString() : '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              :loading="replaying === row.id"
              @click="replayOne(row)"
            >
              放回队列
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pic-muted" style="margin-top: 8px">
        与「工作台 → 任务 → 重试」的分工：<b>重试</b>面向任务（新建一条队列项、任务侧重试次数累加）；
        <b>放回队列</b>面向队列项（复用这条死信本身，保留错误原因）。两者都只动队列，
        不会绕过权限与人工审核。
      </div>
    </el-card>

    <el-card shadow="never" class="pic-section-gap">
      <template #header><span style="font-weight: 600">限额、预算与缓存</span></template>
      <el-table :data="limitRows" size="small">
        <el-table-column prop="label" label="项" width="220" />
        <el-table-column prop="value" label="值" />
      </el-table>

      <el-descriptions :column="3" border size="small" style="margin-top: 12px">
        <el-descriptions-item label="缓存后端">
          <el-tag size="small" :type="health?.cache?.healthy ? 'success' : 'warning'">
            {{ health?.cache?.backend }}
          </el-tag>
          <el-tag
            v-if="health?.cache?.persistent"
            size="small"
            type="success"
            effect="plain"
            style="margin-left: 6px"
          >
            落盘 · 跨重启保留
          </el-tag>
          <span v-if="health?.cache?.healthy === false" class="pic-muted" style="margin-left: 8px">
            {{ health?.cache?.degraded_reason || '已降级' }}
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="缓存命中率（累计）">
          {{ ((health?.cache?.hit_rate ?? 0) * 100).toFixed(0) }}%（命中 {{ health?.cache?.hits }} / 未命中 {{ health?.cache?.misses }}）
        </el-descriptions-item>
        <el-descriptions-item :label="`缓存命中率（最近 ${health?.cache?.recent?.window ?? 0} 次）`">
          <span style="font-weight: 600">{{ ((health?.cache?.recent?.hit_rate ?? 0) * 100).toFixed(0) }}%</span>
          <span class="pic-muted" style="margin-left: 6px">这才是"现在管不管用"</span>
        </el-descriptions-item>
        <el-descriptions-item label="落盘缓存条目">
          {{ health?.cache?.entries ?? '—' }}
          <span v-if="health?.cache?.path" class="pic-muted" style="margin-left: 6px">
            {{ health?.cache?.path?.split(/[\\/]/).pop() }}
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="向量缓存（查询向量）">
          <template v-if="health?.cache?.vector_cache?.enabled">
            {{ ((health?.cache?.vector_cache?.hit_rate ?? 0) * 100).toFixed(0) }}%
            <span class="pic-muted">
              · {{ health?.cache?.vector_cache?.entries }}/{{ health?.cache?.vector_cache?.max_entries }} 条
            </span>
          </template>
          <template v-else><span class="pic-muted">已关闭</span></template>
        </el-descriptions-item>
        <el-descriptions-item label="已登记单价的模型">
          {{ health?.cost?.priced_models?.join('、') || '—' }}
        </el-descriptions-item>
      </el-descriptions>

      <el-table
        v-if="cacheNamespaceRows.length"
        :data="cacheNamespaceRows"
        size="small"
        style="margin-top: 12px"
      >
        <el-table-column prop="label" label="缓存层" width="150" />
        <el-table-column prop="hits" label="命中" width="90" />
        <el-table-column prop="lookups" label="查找" width="90" />
        <el-table-column prop="rate" label="命中率" width="100" />
        <el-table-column prop="note" label="怎么看" />
      </el-table>

      <div class="pic-muted" style="margin-top: 8px">
        为什么要分层看：累计命中率会被“很久以前的一次冷启动”长期拖累 ——
        刚重启完跑一个新问题时检索键全是新的，命中率必然接近 0，这是预期而不是故障；
        同一任务内多个 Agent 查同一指标、问同一段查询词时才会命中。
        要看"现在到底管不管用"，请看<b>最近窗口</b>那一个数字。
      </div>
    </el-card>

    <el-card shadow="never" class="pic-section-gap">
      <template #header><span style="font-weight: 600">成本（近 7 天）</span></template>
      <el-row :gutter="16">
        <el-col :span="6">
          <div class="pic-muted">今日成本</div>
          <div style="font-size: 20px; font-weight: 600">
            {{ costs?.today.amount ?? 0 }} {{ costs?.today.currency || costs?.cost.currency }}
          </div>
          <div class="pic-muted">限额 {{ costs?.today.limit || '不限' }}</div>
        </el-col>
        <el-col :span="6">
          <div class="pic-muted">窗口金额</div>
          <div style="font-size: 20px; font-weight: 600">{{ costs?.cost.amount ?? 0 }}</div>
          <div class="pic-muted">{{ costs?.cost.calls ?? 0 }} 次模型调用</div>
        </el-col>
        <el-col :span="6">
          <div class="pic-muted">Token 合计</div>
          <div style="font-size: 20px; font-weight: 600">{{ costs?.cost.total_tokens ?? 0 }}</div>
          <div class="pic-muted">缓存命中 {{ costs?.cost.cache_hit_tokens ?? 0 }}</div>
        </el-col>
        <el-col :span="6">
          <div class="pic-muted">未计价模型</div>
          <div style="font-size: 20px; font-weight: 600">{{ costs?.cost.unpriced_models?.length ?? 0 }}</div>
          <div class="pic-muted">{{ costs?.cost.unpriced_models?.join('、') || '无' }}</div>
        </el-col>
      </el-row>

      <el-table v-if="costs && Object.keys(costs.cost.per_model).length" :data="Object.entries(costs.cost.per_model).map(([model, v]) => ({ model, ...v }))" size="small" style="margin-top: 12px">
        <el-table-column prop="model" label="模型" width="220" />
        <el-table-column prop="calls" label="调用次数" width="110" />
        <el-table-column prop="input_tokens" label="输入 token" width="130" />
        <el-table-column prop="output_tokens" label="输出 token" width="130" />
        <el-table-column prop="amount" label="金额" />
      </el-table>
      <div class="pic-muted" style="margin-top: 8px">
        未配置真实模型时金额恒为 0 —— 这是正常的，不代表统计失效。
      </div>
    </el-card>

    <el-card shadow="never" class="pic-section-gap">
      <template #header>
        <span style="font-weight: 600">反思灰度与发布门禁</span>
        <el-tag
          v-if="reflectionRelease"
          :type="releaseTagType"
          size="small"
          style="margin-left: 10px"
        >
          {{ reflectionRelease.decision.recommendation }}
        </el-tag>
      </template>

      <el-alert
        v-if="health?.reflection?.enabled === false"
        type="info"
        :closable="false"
        title="反思总开关当前关闭"
        description="发布比例会保留，但实际任务不会进入反思流程；开启总开关后才按下方范围和比例生效。"
        style="margin-bottom: 12px"
      />

      <el-descriptions v-if="reflectionRelease" :column="3" border size="small">
        <el-descriptions-item label="当前覆盖">
          <b>{{ reflectionRelease.state.current_percent }}%</b>
          <span class="pic-muted" style="margin-left: 6px">{{ reflectionRelease.state.status }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="系统建议">
          {{ reflectionRelease.decision.recommendation }}
          <span v-if="reflectionRelease.decision.recommendation === 'promote'">
            → {{ reflectionRelease.decision.recommended_percent }}%
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="样本门槛">
          每组至少 {{ reflectionRelease.config.min_sample }} 个任务
        </el-descriptions-item>
        <el-descriptions-item label="租户范围">
          {{ scopeText(reflectionRelease.scope.tenants) }}
        </el-descriptions-item>
        <el-descriptions-item label="风险范围">
          {{ scopeText(reflectionRelease.scope.risk_levels) }}
        </el-descriptions-item>
        <el-descriptions-item label="问题范围">
          {{ scopeText(reflectionRelease.scope.issue_types) }}
        </el-descriptions-item>
      </el-descriptions>

      <el-alert
        v-if="reflectionRelease"
        :type="releaseTagType"
        :closable="false"
        :title="reflectionRelease.decision.reason"
        style="margin-top: 12px"
      />

      <el-row v-if="reflectionRelease" :gutter="12" style="margin-top: 12px">
        <el-col :span="6">
          <div class="pic-muted">反思组 / 对照组</div>
          <b>{{ reflectionRelease.decision.metrics.treatment_sample }} / {{ reflectionRelease.decision.metrics.control_sample }}</b>
        </el-col>
        <el-col :span="6">
          <div class="pic-muted">安全违规率</div>
          <b>{{ (reflectionRelease.decision.metrics.safety_violation_rate * 100).toFixed(1) }}%</b>
        </el-col>
        <el-col :span="6">
          <div class="pic-muted">有界终止率</div>
          <b>{{ (reflectionRelease.decision.metrics.bounded_termination_rate * 100).toFixed(1) }}%</b>
        </el-col>
        <el-col :span="6">
          <div class="pic-muted">反思组 P95 耗时</div>
          <b>{{ reflectionRelease.decision.metrics.treatment_p95_latency_ms.toFixed(0) }} ms</b>
        </el-col>
      </el-row>

      <div v-if="canAdmin && reflectionRelease" style="margin-top: 14px">
        <el-button
          v-if="reflectionRelease.decision.recommendation === 'promote'"
          type="primary"
          :loading="releaseAction === 'approve_promotion'"
          @click="changeReflectionRelease('approve_promotion', reflectionRelease.decision.recommended_percent)"
        >
          批准扩大到 {{ reflectionRelease.decision.recommended_percent }}%
        </el-button>
        <el-button :loading="releaseAction === 'evaluate'" @click="changeReflectionRelease('evaluate')">
          重新评估
        </el-button>
        <el-button
          v-if="reflectionRelease.state.status === 'active'"
          :loading="releaseAction === 'pause'"
          @click="changeReflectionRelease('pause')"
        >
          暂停扩大
        </el-button>
        <el-button
          v-else
          :loading="releaseAction === 'resume'"
          @click="changeReflectionRelease('resume')"
        >
          恢复灰度
        </el-button>
        <el-button
          type="danger"
          plain
          :disabled="reflectionRelease.state.current_percent === 0"
          :loading="releaseAction === 'rollback'"
          @click="changeReflectionRelease('rollback')"
        >
          回滚到 0%
        </el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="pic-section-gap">
      <template #header>
        <span style="font-weight: 600">模型路由与灰度</span>
        <span class="pic-muted" style="margin-left: 10px">
          灰度门槛：每侧 ≥ {{ health?.llm?.canary_experiment?.min_sample ?? '—' }} 个任务才给结论
        </span>
      </template>
      <el-table :data="health?.llm?.routing ?? []" size="small">
        <el-table-column prop="key" label="Agent / 节点" width="220" />
        <el-table-column prop="model" label="模型" width="220" />
        <el-table-column prop="source" label="来源" width="120" />
        <el-table-column label="可用">
          <template #default="{ row }">
            <el-tag size="small" :type="row.configured ? 'success' : 'info'">{{ row.configured ? '已配置' : '未配置（降级）' }}</el-tag>
          </template>
        </el-table-column>
      </el-table>

      <el-alert
        v-if="!(health?.llm?.canary?.length)"
        type="info"
        :closable="false"
        style="margin-top: 12px"
        title="当前未开启模型灰度（MODEL_CANARY 未配置）"
        description="开启后这里会显示候选模型与流量比例，工作台会出现灰度效果对比面板。"
      />
      <el-table v-else :data="health?.llm?.canary ?? []" size="small" style="margin-top: 12px">
        <el-table-column prop="key" label="Agent" width="200" />
        <el-table-column prop="model" label="候选模型" width="220" />
        <el-table-column prop="percent" label="流量比例" width="120" />
        <el-table-column label="状态">
          <template #default="{ row }">
            <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '生效中' : '未生效' }}</el-tag>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="canary" style="margin-top: 16px">
        <el-divider content-position="left">灰度效果对比（近 {{ canary.window_days }} 天）</el-divider>
        <el-tag :type="canary.verdict === '样本不足' ? 'info' : 'success'" size="small">{{ canary.verdict }}</el-tag>
        <span class="pic-muted" style="margin-left: 10px">{{ canary.verdict_reason }}</span>
        <el-table v-if="canaryRows.length" :data="canaryRows" size="small" style="margin-top: 8px">
          <el-table-column prop="metric" label="指标" width="210" />
          <el-table-column prop="canary" label="灰度" width="100" />
          <el-table-column prop="control" label="对照" width="100" />
          <el-table-column prop="deltaText" label="差异" width="100" />
          <el-table-column prop="verdict" label="结论" width="120" />
          <el-table-column prop="note" label="依据" min-width="200" />
        </el-table>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.proactive-trigger {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 14px;
  padding: 14px 16px;
  border: 1px solid var(--el-border-color-light);
  border-left: 3px solid var(--el-color-primary);
  background: var(--el-fill-color-extra-light);
}

.proactive-trigger__title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}

.proactive-trigger p {
  max-width: 780px;
  margin: 6px 0;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

@media (max-width: 720px) {
  .proactive-trigger {
    flex-direction: column;
  }
}
</style>

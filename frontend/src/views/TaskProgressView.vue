<script setup lang="ts">
/** 任务进度：工作流节点、Agent 状态、耗时、重试、人工等待与事件流。 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import StatusTag from '@/components/StatusTag.vue'
import RiskTag from '@/components/RiskTag.vue'
import WorkflowTimeline from '@/components/WorkflowTimeline.vue'
import AgentCollaborationPanel from '@/components/AgentCollaborationPanel.vue'
import { systemApi, taskApi } from '@/api/endpoints'
import type { TaskEventOut, TaskLiveSnapshot, TaskOut, TaskTrace, WorkflowInfo } from '@/api/types'
import { useSessionStore } from '@/stores/session'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()

const taskId = computed(() => String(route.params.id))
const task = ref<TaskOut | null>(null)
const trace = ref<TaskTrace | null>(null)
const snapshot = ref<TaskLiveSnapshot | null>(null)
const events = ref<TaskEventOut[]>([])
const workflow = ref<WorkflowInfo | null>(null)
const loading = ref(true)
const reviewing = ref(false)
const reviewComment = ref('')

let timer: number | undefined
const isLive = computed(() => ['pending', 'running', 'waiting_human'].includes(task.value?.status ?? ''))

async function loadAll(): Promise<void> {
  try {
    const [t, tr, ev, live] = await Promise.all([
      taskApi.get(taskId.value),
      taskApi.trace(taskId.value),
      taskApi.events(taskId.value),
      taskApi.snapshot(taskId.value).catch(() => null),
    ])
    task.value = t
    trace.value = tr
    events.value = ev.items
    snapshot.value = live
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

async function pollOnce(): Promise<void> {
  try {
    const [t, tr, live] = await Promise.all([
      taskApi.get(taskId.value),
      taskApi.trace(taskId.value),
      taskApi.snapshot(taskId.value).catch(() => null),
    ])
    task.value = t
    trace.value = tr
    snapshot.value = live
    if (t.status === 'running' || t.status === 'pending') {
      const last = events.value[events.value.length - 1]
      const ev = await taskApi.events(taskId.value, last?.id)
      if (ev.items.length) events.value = [...events.value, ...ev.items]
    } else {
      const ev = await taskApi.events(taskId.value)
      events.value = ev.items
    }
  } catch {
    /* 轮询失败不打扰用户，下一轮再试 */
  }
}

async function cancel(): Promise<void> {
  try {
    await ElMessageBox.confirm('取消后工作流会在当前节点边界停止，确认取消？', '取消任务', { type: 'warning' })
    await taskApi.cancel(taskId.value, task.value?.state_revision)
    ElMessage.success('已提交取消请求')
    await pollOnce()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error((error as Error).message)
  }
}

async function retry(): Promise<void> {
  try {
    await taskApi.retry(taskId.value, '进度页手动重试', task.value?.state_revision)
    ElMessage.success('已重新提交执行')
    await pollOnce()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function decide(action: 'approve' | 'reject' | 'modify'): Promise<void> {
  reviewing.value = true
  try {
    if (action === 'approve' || action === 'reject') {
      await ElMessageBox.confirm(
        action === 'approve'
          ? '确认按现有结论出具报告？'
          : '驳回后仍会保留结论与证据作为过程留痕，但不会作为决策依据。确认驳回？',
        action === 'approve' ? '审核通过' : '驳回',
        { type: action === 'approve' ? 'info' : 'warning' },
      )
    }
    await taskApi.approve(
      taskId.value,
      action,
      reviewComment.value || undefined,
      undefined,
      task.value?.state_revision,
    )
    ElMessage.success('审核结论已提交，工作流正在恢复')
    reviewComment.value = ''
    await pollOnce()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error((error as Error).message)
  } finally {
    reviewing.value = false
  }
}

function formatMs(value?: number | null): string {
  if (!value) return '—'
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${value}ms`
}

onMounted(async () => {
  workflow.value = await systemApi.workflow().catch(() => null)
  await loadAll()
  timer = window.setInterval(() => {
    if (isLive.value) void pollOnce()
  }, 2000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})

watch(taskId, loadAll)
</script>

<template>
  <div v-loading="loading">
    <div class="pic-page-title">
      <h2>任务进度</h2>
      <div style="display: flex; gap: 8px">
        <el-button link type="primary" @click="router.push({ name: 'workbench' })">返回工作台</el-button>
        <el-button :icon="'Refresh'" @click="loadAll">刷新</el-button>
      </div>
    </div>

    <el-card v-if="task" shadow="never">
      <div style="display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap">
        <div style="flex: 1; min-width: 320px">
          <div style="font-size: 15px; line-height: 1.7">{{ task.query }}</div>
          <div class="pic-muted" style="margin-top: 6px">
            任务 ID <span class="pic-mono">{{ task.id }}</span> ·
            工作流 {{ task.workflow_version || '—' }} · Prompt {{ task.prompt_version || '—' }}
          </div>
        </div>
        <div style="text-align: right; display: flex; flex-direction: column; gap: 8px; align-items: flex-end">
          <StatusTag :status="task.status" :retry-count="task.retry_count" :waiting-reason="task.waiting_reason" />
          <RiskTag :risk="task.risk_level" />
          <div style="display: flex; gap: 8px">
            <el-button
              v-if="['pending', 'running', 'waiting_human'].includes(task.status)"
              size="small"
              @click="cancel"
            >
              取消
            </el-button>
            <el-button
              v-if="['failed', 'cancelled'].includes(task.status)"
              size="small"
              type="warning"
              @click="retry"
            >
              重试
            </el-button>
            <el-button
              v-if="task.report_id"
              size="small"
              type="success"
              @click="router.push({ name: 'report', params: { id: task.id } })"
            >
              查看报告
            </el-button>
          </div>
        </div>
      </div>

      <el-alert
        v-if="task.status === 'waiting_human'"
        type="warning"
        :closable="false"
        show-icon
        class="pic-section-gap"
        :title="`等待人工审核：${task.waiting_reason || '高风险或存在冲突'}`"
      />
      <el-alert
        v-if="task.status === 'failed' && task.error"
        type="error"
        :closable="false"
        show-icon
        class="pic-section-gap"
        :title="`任务失败（${task.error.code}）：${task.error.message}`"
      />

      <AgentCollaborationPanel class="pic-section-gap" :snapshot="snapshot" />

      <el-descriptions :column="4" size="small" border class="pic-section-gap">
        <el-descriptions-item label="意图">{{ task.intent || '—' }}</el-descriptions-item>
        <el-descriptions-item label="当前节点">
          <span class="pic-mono">{{ task.current_node || '—' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="证据条数">{{ task.evidence_count || '—' }}</el-descriptions-item>
        <el-descriptions-item label="重试次数">{{ task.retry_count }}</el-descriptions-item>
        <el-descriptions-item label="开始">{{ task.started_at ? new Date(task.started_at).toLocaleTimeString() : '—' }}</el-descriptions-item>
        <el-descriptions-item label="结束">{{ task.finished_at ? new Date(task.finished_at).toLocaleTimeString() : '—' }}</el-descriptions-item>
        <el-descriptions-item label="Agent 总耗时">{{ formatMs(trace?.totals.latency_ms) }}</el-descriptions-item>
        <el-descriptions-item label="工具调用">
          {{ trace?.totals.tool_calls ?? 0 }} 次（被拒 {{ trace?.totals.denied_tool_calls ?? 0 }}）
        </el-descriptions-item>
        <el-descriptions-item label="Token 用量">
          {{ trace?.cost?.total_tokens ?? trace?.totals.tokens ?? 0 }}
          <span v-if="trace?.cost?.cache_hit_tokens" class="pic-muted">
            （缓存命中 {{ trace.cost.cache_hit_tokens }}）
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="成本">
          <span v-if="trace?.cost">
            {{ trace.cost.amount.toFixed(4) }} {{ trace.cost.currency }}
            <span class="pic-muted">（{{ trace.cost.calls }} 次模型调用）</span>
          </span>
          <span v-else class="pic-muted">—</span>
        </el-descriptions-item>
      </el-descriptions>

      <el-alert
        v-if="trace?.budget?.exceeded"
        type="warning"
        :closable="false"
        show-icon
        class="pic-section-gap"
        :title="`已触及成本预算：${trace.budget.reasons.join('；')}`"
      >
        <div>
          当前动作：{{
            trace.budget.action === 'abort'
              ? '直接中断任务（可重试，或提高预算后重试）'
              : '停止调用模型，改用确定性归纳继续出报告'
          }}
        </div>
      </el-alert>
      <el-alert
        v-if="trace?.cost && trace.cost.unpriced_models.length"
        type="info"
        :closable="false"
        class="pic-section-gap"
        :title="`以下模型未配置单价，成本按兜底价估算：${trace.cost.unpriced_models.join('、')}`"
      />

      <div v-if="session.can('task:approve') && task.status === 'waiting_human'" class="pic-section-gap">
        <el-input
          v-model="reviewComment"
          placeholder="审核意见（可选，会写入审核记录与审计日志）"
          style="margin-bottom: 8px"
        />
        <el-button type="success" :loading="reviewing" @click="decide('approve')">通过并出报告</el-button>
        <el-button type="primary" plain :loading="reviewing" @click="decide('modify')">
          修改后通过
        </el-button>
        <el-button type="danger" plain :loading="reviewing" @click="decide('reject')">驳回</el-button>
        <span class="pic-muted" style="margin-left: 8px">
          「修改后通过」请在报告页填写修改内容
        </span>
      </div>
    </el-card>

    <el-row :gutter="16" class="pic-section-gap">
      <el-col :span="10">
        <WorkflowTimeline
          v-if="workflow && task"
          :nodes="workflow.nodes"
          :events="events"
          :current-node="task.current_node"
          :status="task.status"
        />
      </el-col>
      <el-col :span="14">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between">
              <span style="font-weight: 600">Agent 执行情况</span>
              <span class="pic-muted">Token 合计 {{ trace?.totals.tokens ?? 0 }}</span>
            </div>
          </template>
          <el-table :data="trace?.agent_runs ?? []" size="small">
            <el-table-column prop="agent_code" label="Agent" width="150" />
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="row.status === 'succeeded' ? 'success' : 'danger'">
                  {{ row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="结论" width="70">
              <template #default="{ row }">{{ row.findings }}</template>
            </el-table-column>
            <el-table-column label="耗时" width="90">
              <template #default="{ row }">{{ formatMs(row.latency_ms) }}</template>
            </el-table-column>
            <el-table-column label="模型" width="190">
              <template #default="{ row }">
                <div>{{ row.model || '—' }}</div>
                <el-tag v-if="row.model_used === false" size="small" type="info" effect="plain">
                  已降级（未用模型）
                </el-tag>
                <!-- 灰度期间要能一眼看出这条记录走了候选模型还是对照模型 -->
                <el-tag
                  v-else-if="row.model_source === 'canary'"
                  size="small"
                  type="warning"
                  effect="plain"
                >
                  灰度模型{{ row.canary_bucket != null ? ` · 桶${row.canary_bucket}` : '' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="摘要" min-width="200">
              <template #default="{ row }">
                <span class="pic-muted">{{ row.summary || '—' }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never" class="pic-section-gap">
          <template #header>
            <span style="font-weight: 600">工具调用（含被策略拒绝的）</span>
          </template>
          <el-table :data="trace?.tool_calls ?? []" size="small" max-height="320">
            <el-table-column prop="tool_name" label="工具" width="130" />
            <el-table-column label="策略" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.policy_decision === 'deny' ? 'danger' : 'success'">
                  {{ row.policy_decision }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="结果" min-width="180">
              <template #default="{ row }">{{ row.result_summary || '—' }}</template>
            </el-table-column>
            <el-table-column label="耗时" width="80">
              <template #default="{ row }">{{ formatMs(row.latency_ms) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="pic-section-gap">
      <template #header>
        <div style="display: flex; justify-content: space-between">
          <span style="font-weight: 600">事件流</span>
          <span class="pic-muted">{{ events.length }} 条{{ isLive ? '（每 2 秒自动刷新）' : '' }}</span>
        </div>
      </template>
      <div class="pic-log">
        <div
          v-for="event in events"
          :key="event.id"
          :class="`pic-log__line--${event.level}`"
        >
          [{{ new Date(event.created_at).toLocaleTimeString() }}] {{ event.node || '系统' }}：{{ event.message }}
        </div>
      </div>
    </el-card>
  </div>
</template>

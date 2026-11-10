<script setup lang="ts">
import { computed } from 'vue'
import type { TaskExecutionUnit, TaskLiveSnapshot } from '@/api/types'

const props = defineProps<{
  snapshot: TaskLiveSnapshot | null
}>()

type DisplayUnit = TaskExecutionUnit & {
  tools: TaskExecutionUnit[]
}

const statusLabels: Record<string, string> = {
  pending: '等待开始',
  running: '正在处理',
  waiting: '等待确认',
  cancel_requested: '正在取消',
  succeeded: '已完成',
  failed: '处理失败',
  cancelled: '已取消',
  rejected: '已驳回',
  skipped: '已跳过',
}

const phaseLabels: Record<string, string> = {
  intake: '理解问题',
  planning: '拆解任务',
  evidence_collection: '收集证据',
  analysis: '并行分析',
  synthesis: '整理结论',
  reflection: '复核结论',
  human_review: '等待人工确认',
  persistence: '保存结果',
  completed: '任务完成',
  unknown: '处理中',
}

const agentProfiles: Record<string, { name: string; responsibility: string }> = {
  product_agent: { name: '产品知识 Agent', responsibility: '核对产品规则、功能口径与历史决策' },
  feedback_agent: { name: '用户反馈 Agent', responsibility: '归纳用户反馈、投诉主题与影响范围' },
  release_agent: { name: '版本研发 Agent', responsibility: '核对版本变更、发布时间与研发记录' },
  metric_agent: { name: '数据分析 Agent', responsibility: '分析指标变化、样本质量与异常区间' },
  order_fact_agent: { name: '订单事实 Agent', responsibility: '查询订单、支付、物流与售后事实' },
  policy_rag_agent: { name: '政策核验 Agent', responsibility: '检索适用政策并核对生效范围' },
  triage_agent: { name: '问题分诊 Agent', responsibility: '识别问题类型、优先级与处理路径' },
  followup_agent: { name: '信息补全 Agent', responsibility: '识别缺失信息并生成补充问题' },
  resolution_agent: { name: '解决方案 Agent', responsibility: '形成可执行的处理建议与风险边界' },
  response_agent: { name: '客户回复 Agent', responsibility: '整理面向客户的回复内容' },
  report_agent: { name: '报告整理 Agent', responsibility: '合并多方证据并生成诊断结论' },
  review_agent: { name: '结论复核 Agent', responsibility: '检查证据充分性、冲突与遗漏' },
}

function collectUnits(root: TaskExecutionUnit | null, type: TaskExecutionUnit['unit_type']): TaskExecutionUnit[] {
  if (!root) return []
  const result: TaskExecutionUnit[] = []
  const visit = (unit: TaskExecutionUnit): void => {
    if (unit.unit_type === type) result.push(unit)
    unit.children.forEach(visit)
  }
  visit(root)
  return result
}

function metadataText(unit: TaskExecutionUnit, key: string): string {
  const value = unit.metadata[key]
  return typeof value === 'string' ? value : ''
}

function agentCode(unit: TaskExecutionUnit): string {
  return metadataText(unit, 'agent_code') || unit.name
}

function profile(unit: TaskExecutionUnit): { name: string; responsibility: string } {
  const code = agentCode(unit)
  return agentProfiles[code] || {
    name: unit.name || '协作 Agent',
    responsibility: '完成当前分配的专项核验任务',
  }
}

function statusLabel(status: string): string {
  return statusLabels[status] || status
}

function statusClass(status: string): string {
  if (status === 'succeeded') return 'is-success'
  if (status === 'running') return 'is-running'
  if (status === 'waiting') return 'is-waiting'
  if (['failed', 'rejected'].includes(status)) return 'is-danger'
  return 'is-neutral'
}

function workSummary(unit: DisplayUnit): string {
  const item = profile(unit)
  const toolCount = unit.tools.length
  if (unit.status === 'running') return `正在执行：${item.responsibility}`
  if (unit.status === 'waiting') return `当前任务需要外部确认后才能继续：${item.responsibility}`
  if (unit.status === 'succeeded') {
    return toolCount > 0
      ? `已完成职责范围内的核验，共使用 ${toolCount} 个工具取得依据。`
      : '已完成职责范围内的分析，并已把结果交给后续环节。'
  }
  if (unit.status === 'failed') return metadataText(unit, 'error') || '本环节未能完成，可在技术记录中查看失败信息。'
  return item.responsibility
}

function toolResult(unit: TaskExecutionUnit): string {
  const call = props.snapshot?.tool_calls.find((item) => item.id === unit.owner_ref_id)
  if (!call) return statusLabel(unit.status)
  return call.result_summary || statusLabel(call.status)
}

const agents = computed<DisplayUnit[]>(() =>
  collectUnits(props.snapshot?.task_tree || null, 'agent').map((unit) => ({
    ...unit,
    tools: unit.children.filter((child) => child.unit_type === 'tool'),
  })),
)

const approvals = computed(() => collectUnits(props.snapshot?.task_tree || null, 'approval'))

const summary = computed(() => {
  const rows = agents.value
  return {
    total: rows.length,
    running: rows.filter((item) => item.status === 'running').length,
    completed: rows.filter((item) => item.status === 'succeeded').length,
    waiting: rows.filter((item) => item.status === 'waiting').length + approvals.value.filter((item) => item.status === 'waiting').length,
    failed: rows.filter((item) => ['failed', 'rejected'].includes(item.status)).length,
  }
})

const currentPhase = computed(() => {
  const phase = props.snapshot?.task.current_phase || 'unknown'
  return phaseLabels[phase] || phase
})
</script>

<template>
  <section data-testid="agent-collaboration" class="agent-collaboration" aria-labelledby="agent-collaboration-title">
    <header class="collaboration-header">
      <div>
        <div class="section-kicker">多 Agent 协作</div>
        <h3 id="agent-collaboration-title">当前由谁处理、处理到哪里</h3>
        <p>每个 Agent 只负责一类问题，结果会经过汇总和复核后再形成最终结论。</p>
      </div>
      <div class="phase-indicator">
        <span>当前阶段</span>
        <strong>{{ currentPhase }}</strong>
      </div>
    </header>

    <div class="collaboration-summary" aria-label="协作进度">
      <div class="summary-item">
        <span>参与 Agent</span>
        <strong>{{ summary.total }}</strong>
      </div>
      <div class="summary-item is-running">
        <span>正在处理</span>
        <strong>{{ summary.running }}</strong>
      </div>
      <div class="summary-item is-success">
        <span>已完成</span>
        <strong>{{ summary.completed }}</strong>
      </div>
      <div class="summary-item is-waiting">
        <span>等待确认</span>
        <strong>{{ summary.waiting }}</strong>
      </div>
      <div class="summary-item is-danger">
        <span>异常</span>
        <strong>{{ summary.failed }}</strong>
      </div>
    </div>

    <div v-if="agents.length" class="agent-list">
      <article
        v-for="unit in agents"
        :key="unit.id"
        data-testid="agent-collaboration-unit"
        class="agent-unit"
      >
        <div class="agent-unit__heading">
          <div>
            <h4>{{ profile(unit).name }}</h4>
            <p><strong>负责：</strong>{{ profile(unit).responsibility }}</p>
          </div>
          <span class="status-pill" :class="statusClass(unit.status)">{{ statusLabel(unit.status) }}</span>
        </div>

        <div class="agent-unit__work">
          <span>处理内容</span>
          <p>{{ workSummary(unit) }}</p>
        </div>

        <div v-if="unit.tools.length" class="tool-evidence">
          <span>使用依据与工具</span>
          <ul>
            <li v-for="tool in unit.tools" :key="tool.id">
              <strong>{{ tool.name }}</strong>
              <span>{{ toolResult(tool) }}</span>
            </li>
          </ul>
        </div>

        <details class="technical-details">
          <summary>查看技术执行细节</summary>
          <dl>
            <div>
              <dt>Agent code</dt>
              <dd>{{ agentCode(unit) }}</dd>
            </div>
            <div>
              <dt>模型</dt>
              <dd>{{ metadataText(unit, 'model') || '未记录' }}</dd>
            </div>
            <div>
              <dt>执行记录 ID</dt>
              <dd>{{ unit.owner_ref_id }}</dd>
            </div>
            <div>
              <dt>失败处理</dt>
              <dd>{{ unit.failure_policy }}</dd>
            </div>
          </dl>
          <div v-if="unit.tools.length" class="technical-tools">
            <div v-for="tool in unit.tools" :key="tool.id">
              {{ tool.name }} · replay={{ metadataText(tool, 'replay_policy') || '未记录' }} ·
              side_effect={{ metadataText(tool, 'side_effect_level') || '未记录' }}
            </div>
          </div>
        </details>
      </article>
    </div>

    <div v-else class="empty-collaboration">
      <strong>协作任务尚未开始</strong>
      <span>系统完成问题拆解后，会在这里展示各 Agent 的职责与实时进度。</span>
    </div>

    <div v-if="approvals.length" class="approval-list">
      <article v-for="unit in approvals" :key="unit.id" class="approval-unit">
        <div>
          <span class="approval-unit__label">人工审核</span>
          <strong>{{ statusLabel(unit.status) }}</strong>
          <p>{{ metadataText(unit, 'reason') || '系统需要人工确认结论和风险后才能继续。' }}</p>
        </div>
        <details class="technical-details">
          <summary>查看审批记录标识</summary>
          <div class="approval-owner">{{ unit.owner_ref_id }}</div>
        </details>
      </article>
    </div>
  </section>
</template>

<style scoped>
.agent-collaboration {
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  background: var(--el-fill-color-blank);
  overflow: hidden;
}

.collaboration-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding: 20px 22px 18px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  background: var(--el-fill-color-extra-light);
}

.section-kicker {
  margin-bottom: 4px;
  color: var(--el-color-primary);
  font-size: 12px;
  font-weight: 700;
}

.collaboration-header h3,
.agent-unit h4 {
  margin: 0;
  color: var(--el-text-color-primary);
}

.collaboration-header h3 {
  font-size: 17px;
  line-height: 1.5;
}

.collaboration-header p,
.agent-unit p,
.approval-unit p {
  margin: 5px 0 0;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

.phase-indicator {
  min-width: 128px;
  padding-left: 18px;
  border-left: 2px solid var(--el-color-primary-light-5);
}

.phase-indicator span,
.summary-item span,
.agent-unit__work > span,
.tool-evidence > span {
  display: block;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.phase-indicator strong {
  display: block;
  margin-top: 4px;
  font-size: 16px;
}

.collaboration-summary {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.summary-item {
  padding: 14px 18px;
  border-right: 1px solid var(--el-border-color-lighter);
}

.summary-item:last-child {
  border-right: 0;
}

.summary-item strong {
  display: block;
  margin-top: 3px;
  font-size: 19px;
}

.summary-item.is-running strong { color: var(--el-color-primary); }
.summary-item.is-success strong { color: var(--el-color-success); }
.summary-item.is-waiting strong { color: var(--el-color-warning); }
.summary-item.is-danger strong { color: var(--el-color-danger); }

.agent-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.agent-unit {
  min-width: 0;
  padding: 18px 22px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.agent-unit:nth-child(odd) {
  border-right: 1px solid var(--el-border-color-lighter);
}

.agent-unit__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}

.agent-unit h4 {
  font-size: 15px;
  line-height: 1.5;
}

.agent-unit__heading p {
  font-size: 13px;
}

.status-pill {
  flex: 0 0 auto;
  padding: 3px 8px;
  border: 1px solid var(--el-border-color);
  border-radius: 999px;
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-light);
  font-size: 12px;
}

.status-pill.is-running { border-color: var(--el-color-primary-light-5); color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
.status-pill.is-success { border-color: var(--el-color-success-light-5); color: var(--el-color-success); background: var(--el-color-success-light-9); }
.status-pill.is-waiting { border-color: var(--el-color-warning-light-5); color: var(--el-color-warning-dark-2); background: var(--el-color-warning-light-9); }
.status-pill.is-danger { border-color: var(--el-color-danger-light-5); color: var(--el-color-danger); background: var(--el-color-danger-light-9); }

.agent-unit__work,
.tool-evidence {
  margin-top: 14px;
}

.agent-unit__work p {
  margin-top: 3px;
  color: var(--el-text-color-primary);
  font-size: 13px;
}

.tool-evidence ul {
  margin: 7px 0 0;
  padding: 0;
  list-style: none;
}

.tool-evidence li {
  display: flex;
  gap: 8px;
  align-items: baseline;
  margin-top: 5px;
  font-size: 13px;
}

.tool-evidence li strong {
  flex: 0 0 auto;
  font-weight: 600;
}

.tool-evidence li span {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--el-text-color-secondary);
}

.technical-details {
  margin-top: 14px;
  padding-top: 11px;
  border-top: 1px dashed var(--el-border-color);
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.technical-details summary {
  width: fit-content;
  cursor: pointer;
  color: var(--el-color-primary);
  user-select: none;
}

.technical-details dl {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 16px;
  margin: 12px 0 0;
}

.technical-details dl div {
  min-width: 0;
}

.technical-details dt {
  color: var(--el-text-color-placeholder);
}

.technical-details dd {
  margin: 2px 0 0;
  overflow-wrap: anywhere;
  color: var(--el-text-color-regular);
  font-family: var(--pic-font-mono, monospace);
}

.technical-tools,
.approval-owner {
  margin-top: 10px;
  overflow-wrap: anywhere;
  font-family: var(--pic-font-mono, monospace);
  line-height: 1.7;
}

.empty-collaboration {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 24px 22px;
  color: var(--el-text-color-secondary);
}

.empty-collaboration strong {
  color: var(--el-text-color-primary);
}

.approval-list {
  padding: 14px 22px 18px;
  background: var(--el-color-warning-light-9);
}

.approval-unit {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.approval-unit__label {
  display: inline-block;
  margin-right: 8px;
  color: var(--el-color-warning-dark-2);
  font-size: 12px;
  font-weight: 700;
}

.approval-unit .technical-details {
  flex: 0 0 auto;
  margin-top: 0;
  padding-top: 0;
  border-top: 0;
}

@media (max-width: 900px) {
  .collaboration-summary {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .summary-item:nth-child(3) {
    border-right: 0;
  }

  .summary-item:nth-child(n + 4) {
    border-top: 1px solid var(--el-border-color-lighter);
  }

  .agent-list {
    grid-template-columns: 1fr;
  }

  .agent-unit:nth-child(odd) {
    border-right: 0;
  }
}

@media (max-width: 560px) {
  .collaboration-header,
  .approval-unit {
    flex-direction: column;
  }

  .collaboration-header {
    gap: 14px;
    padding: 17px 16px;
  }

  .phase-indicator {
    width: 100%;
    padding: 10px 0 0;
    border-top: 1px solid var(--el-border-color-lighter);
    border-left: 0;
  }

  .collaboration-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .summary-item,
  .summary-item:nth-child(3) {
    border-right: 1px solid var(--el-border-color-lighter);
  }

  .summary-item:nth-child(even) {
    border-right: 0;
  }

  .summary-item:nth-child(n + 3) {
    border-top: 1px solid var(--el-border-color-lighter);
  }

  .agent-unit,
  .approval-list {
    padding-right: 16px;
    padding-left: 16px;
  }

  .agent-unit__heading {
    gap: 8px;
  }

  .technical-details dl {
    grid-template-columns: 1fr;
  }

  .tool-evidence li {
    align-items: flex-start;
    flex-direction: column;
    gap: 2px;
  }
}
</style>

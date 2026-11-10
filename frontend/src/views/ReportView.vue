<script setup lang="ts">
/**
 * 诊断报告页：结论、影响范围、事实/推断/假设/建议、证据、反证、不确定性、审核操作。
 * 指标曲线通过受控指标接口实时取数（报告只保存指标引用与数据版本，不保存行数据）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import RiskTag from '@/components/RiskTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import StatementList from '@/components/StatementList.vue'
import EvidencePanel from '@/components/EvidencePanel.vue'
import MetricLineChart from '@/components/MetricLineChart.vue'
import { metricApi, reportApi, taskApi } from '@/api/endpoints'
import type { MetricQueryResponse, ReportOut, TaskOut } from '@/api/types'
import { useSessionStore } from '@/stores/session'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()

const taskId = computed(() => String(route.params.id))
const report = ref<ReportOut | null>(null)
const task = ref<TaskOut | null>(null)
const trend = ref<MetricQueryResponse | null>(null)
const trendMetric = ref('')
const loading = ref(true)
const modifying = reactive({ visible: false, summary: '', priority: '', comment: '' })

const evidenceName = (refId?: string): string => {
  if (!refId) return '—'
  const mark = report.value?.citation_marks?.[refId]
  return mark ?? refId
}

const metricCitations = computed(() =>
  (report.value?.citations ?? []).filter((item) => item.source_kind === 'metric'),
)

const docCitations = computed(() =>
  (report.value?.citations ?? []).filter((item) => item.source_kind !== 'metric'),
)

const trendPoints = computed(() =>
  (trend.value?.rows ?? []).map((row) => ({
    label: String(row.stat_date ?? ''),
    value: Number(row.value ?? 0),
  })),
)

async function loadTrend(metricName: string): Promise<void> {
  const filters = (task.value?.filters ?? {}) as Record<string, string>
  if (!metricName || !filters.date_from || !filters.date_to) return
  trendMetric.value = metricName
  try {
    trend.value = await metricApi.trend({
      metric_name: metricName,
      start_date: filters.date_from,
      end_date: filters.date_to,
      platform: filters.platform || undefined,
      app_version: filters.app_version || undefined,
      channel: filters.channel || undefined,
      device: filters.device || undefined,
    })
  } catch (error) {
    ElMessage.warning(`指标取数失败：${(error as Error).message}`)
  }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [t, r] = await Promise.all([
      taskApi.get(taskId.value),
      reportApi.get((await taskApi.get(taskId.value)).report_id ?? ''),
    ]).catch(async () => {
      // 报告可能还没生成：先拿任务，再决定
      const onlyTask = await taskApi.get(taskId.value)
      if (!onlyTask.report_id) throw new Error('该任务还没有生成报告')
      const onlyReport = await reportApi.get(onlyTask.report_id)
      return [onlyTask, onlyReport] as [TaskOut, ReportOut]
    })
    task.value = t as TaskOut
    report.value = r as ReportOut

    const metrics = ((task.value?.filters?.metrics as string[] | undefined) ?? []).slice()
    const metricFromEvidence = (report.value.evidence ?? []).find((item) => item.metric_name)?.metric_name
    const target = metricFromEvidence || metrics[0]
    if (target) await loadTrend(target)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

async function exportFile(format: 'md' | 'json'): Promise<void> {
  if (!report.value) return
  try {
    const content =
      format === 'md'
        ? await reportApi.exportMarkdown(report.value.id)
        : await reportApi.exportJson(report.value.id)
    const blob = new Blob([content], {
      type: format === 'md' ? 'text/markdown;charset=utf-8' : 'application/json;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `诊断报告-${report.value.id.slice(0, 8)}.${format}`
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function decide(action: 'approve' | 'reject' | 'modify'): Promise<void> {
  try {
    const modifications =
      action === 'modify'
        ? {
            ...(modifying.summary ? { summary: modifying.summary } : {}),
            ...(modifying.priority ? { priority: modifying.priority } : {}),
          }
        : undefined
    await taskApi.approve(
      taskId.value,
      action,
      modifying.comment || undefined,
      modifications,
      task.value?.state_revision,
    )
    modifying.visible = false
    ElMessage.success(
      action === 'reject' ? '已驳回（结论保留留痕）' : '审核结论已提交，工作流正在恢复',
    )
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <div class="pic-page-title">
      <h2>诊断报告</h2>
      <div style="display: flex; gap: 8px">
        <el-button link type="primary" @click="router.push({ name: 'task-progress', params: { id: taskId } })">
          任务进度
        </el-button>
        <el-button :disabled="!report" @click="exportFile('md')">导出 Markdown</el-button>
        <el-button :disabled="!report" @click="exportFile('json')">导出 JSON</el-button>
      </div>
    </div>

    <el-empty v-if="!report && !loading" description="该任务还没有生成报告" />

    <template v-if="report && task">
      <el-card shadow="never">
        <div style="display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap">
          <div style="flex: 1; min-width: 320px">
            <div style="font-size: 15px; line-height: 1.7">{{ task.query }}</div>
            <div class="pic-muted" style="margin-top: 6px">
              报告 v{{ report.version }} · 生成于 {{ new Date(report.created_at).toLocaleString() }} ·
              任务 <span class="pic-mono">{{ task.id }}</span>
            </div>
          </div>
          <div style="display: flex; flex-direction: column; gap: 8px; align-items: flex-end">
            <StatusTag :status="task.status" :waiting-reason="task.waiting_reason" />
            <RiskTag :risk="report.risk_level" :priority="report.priority" :confidence="report.confidence" />
            <el-tag
              size="small"
              :type="report.approval_status === 'approved' ? 'success' : report.approval_status === 'rejected' ? 'danger' : 'warning'"
            >
              审核：{{ report.approval_status }}
            </el-tag>
          </div>
        </div>

        <el-alert
          v-if="report.needs_human_review && task.status !== 'waiting_human'"
          class="pic-section-gap"
          type="warning"
          :closable="false"
          show-icon
          title="该报告被标记为需要人工复核：结论在人工确认前不作为决策依据。"
        />

        <el-alert
          v-if="report.reflection"
          class="pic-section-gap"
          :type="report.reflection.verdict === 'human_review' ? 'warning' : 'success'"
          :closable="false"
          show-icon
          :title="report.reflection.label"
        >
          <template #default>
            <span v-if="report.reflection.enabled">
              共复核 {{ report.reflection.rounds }} 次；发现 {{ report.reflection.issue_count }} 项问题。
            </span>
            <details v-if="report.reflection.issues.length" style="margin-top: 6px">
              <summary style="cursor: pointer">查看问题清单</summary>
              <ul style="margin: 6px 0 0; padding-left: 18px">
                <li v-for="issue in report.reflection.issues" :key="issue.code">{{ issue.message }}</li>
              </ul>
            </details>
          </template>
        </el-alert>

        <div v-if="session.can('task:approve') && task.status === 'waiting_human'" class="pic-section-gap">
          <el-alert
            type="warning"
            :closable="false"
            show-icon
            :title="`工作流正在等待人工审核：${task.waiting_reason || '高风险或存在冲突'}`"
          />
          <div style="margin-top: 10px">
            <el-input v-model="modifying.comment" placeholder="审核意见（可选）" style="margin-bottom: 8px" />
            <el-button type="success" @click="decide('approve')">通过</el-button>
            <el-button type="primary" plain @click="modifying.visible = true">修改后通过</el-button>
            <el-button type="danger" plain @click="decide('reject')">驳回</el-button>
          </div>
        </div>
      </el-card>

      <el-card shadow="never" class="pic-section-gap">
        <template #header><span style="font-weight: 600">结论摘要</span></template>
        <p class="pic-pre">{{ report.summary }}</p>
      </el-card>

      <el-card v-if="report.impact_scope.length" shadow="never" class="pic-section-gap">
        <template #header><span style="font-weight: 600">影响范围</span></template>
        <el-table :data="report.impact_scope" size="small">
          <el-table-column prop="dimension" label="维度" width="140" />
          <el-table-column prop="value" label="取值" min-width="240" />
          <el-table-column label="证据" min-width="220">
            <template #default="{ row }">
              <span v-for="ref in row.evidence_refs ?? []" :key="ref" class="pic-citation">
                {{ evidenceName(ref) }}
              </span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <StatementList
        title="事实（有直接证据支持）"
        kind="fact"
        :statements="report.facts"
        :marks="report.citation_marks"
        empty-text="没有可引用的事实"
      />
      <StatementList
        title="推断（由多个事实组合得到）"
        kind="inference"
        :statements="report.inferences"
        :marks="report.citation_marks"
        empty-text="本次没有形成推断"
      />
      <StatementList
        title="假设（证据不足但值得验证）"
        kind="hypothesis"
        :statements="report.hypotheses"
        :marks="report.citation_marks"
        empty-text="本次没有提出假设"
      />
      <StatementList
        title="建议（基于事实与风险的行动项）"
        kind="recommendation"
        :statements="report.recommendations"
        :marks="report.citation_marks"
        empty-text="本次没有给出建议"
      />

      <el-card shadow="never" class="pic-section-gap">
        <template #header>
          <div style="display: flex; justify-content: space-between">
            <span style="font-weight: 600">指标事实</span>
            <el-select
              v-if="((task.filters.metrics as string[]) ?? []).length"
              :model-value="trendMetric"
              size="small"
              style="width: 220px"
              @change="loadTrend"
            >
              <el-option
                v-for="metric in (task.filters.metrics as string[]) ?? []"
                :key="metric"
                :label="metric"
                :value="metric"
              />
            </el-select>
          </div>
        </template>

        <div v-if="trend">
          <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 8px">
            <el-tag size="small" type="info">数据版本 {{ trend.dataset_version || '—' }}</el-tag>
            <el-tag size="small" type="info">来源 {{ trend.source_version || '—' }}</el-tag>
            <el-tag v-if="trend.quality_flags.length" size="small" type="warning">
              质量标记 {{ trend.quality_flags.join('、') }}
            </el-tag>
            <el-tag v-if="!trend.available" size="small" type="danger">该切片无数据</el-tag>
          </div>
          <MetricLineChart :points="trendPoints" />
          <el-alert
            v-for="warning in trend.warnings"
            :key="warning"
            type="warning"
            :closable="false"
            :title="warning"
            class="pic-section-gap"
          />
        </div>
        <el-empty v-else description="该任务未声明指标范围" :image-size="60" />

        <el-descriptions v-if="metricCitations.length" :column="1" size="small" border class="pic-section-gap">
          <el-descriptions-item
            v-for="item in metricCitations.slice(0, 6)"
            :key="item.id"
            :label="item.metric_ref?.metric_name || '指标'"
          >
            <div>
              能力 <span class="pic-mono">{{ item.metric_ref?.capability }}</span> ·
              数据版本 {{ item.metric_ref?.dataset_version || '—' }} ·
              语句 <span class="pic-mono">{{ item.statement_id }}</span>
            </div>
            <div class="pic-mono pic-muted">
              {{ JSON.stringify(item.metric_ref?.params ?? {}) }}
            </div>
          </el-descriptions-item>
        </el-descriptions>
      </el-card>

      <el-card shadow="never" class="pic-section-gap">
        <template #header>
          <div style="display: flex; justify-content: space-between">
            <span style="font-weight: 600">证据冲突与反证（未强行合并）</span>
            <span class="pic-muted">{{ report.conflicts.length }} 处</span>
          </div>
        </template>

        <el-empty v-if="!report.conflicts.length" description="本次未检出跨来源冲突" :image-size="60" />

        <div v-for="conflict in report.conflicts" :key="conflict.id" style="margin-bottom: 16px">
          <el-tag type="danger" effect="dark" size="small">{{ conflict.topic }}</el-tag>
          <span class="pic-muted" style="margin-left: 8px">
            来源类型：{{ (conflict.source_kinds ?? []).join(' / ') || '—' }}
          </span>

          <el-row :gutter="12" style="margin-top: 8px">
            <el-col :span="12">
              <el-alert
                type="info"
                :closable="false"
                :title="`来源 A · ${conflict.statement_a.agent_code || '未知 Agent'}`"
              >
                <div class="pic-pre">{{ conflict.statement_a.text }}</div>
              </el-alert>
            </el-col>
            <el-col :span="12">
              <el-alert
                type="info"
                :closable="false"
                :title="`来源 B · ${conflict.statement_b.agent_code || '未知 Agent'}`"
              >
                <div class="pic-pre">{{ conflict.statement_b.text }}</div>
              </el-alert>
            </el-col>
          </el-row>

          <el-alert
            v-if="conflict.resolution"
            type="success"
            :closable="false"
            show-icon
            style="margin-top: 8px"
            :title="`采用 ${conflict.resolution.adopted_statement}（${conflict.resolution.adopted_source}）`"
          >
            <div>{{ conflict.resolution.reason }}</div>
            <div class="pic-muted">来源优先级规则：{{ conflict.resolution.priority_rule }}</div>
          </el-alert>
          <el-alert
            v-else
            type="warning"
            :closable="false"
            show-icon
            style="margin-top: 8px"
            title="尚未裁决：需要人工确认采用哪一侧"
          />
        </div>
      </el-card>

      <el-card
        v-if="report.data_gaps.length || report.warnings.length"
        shadow="never"
        class="pic-section-gap"
      >
        <template #header><span style="font-weight: 600">不确定性与数据缺口</span></template>
        <ul style="margin: 0; padding-left: 18px; line-height: 1.9">
          <li v-for="gap in report.data_gaps" :key="`gap-${gap}`">数据缺口：{{ gap }}</li>
          <li v-for="warning in report.warnings" :key="`warn-${warning}`">{{ warning }}</li>
        </ul>
      </el-card>

      <el-card shadow="never" class="pic-section-gap">
        <template #header>
          <div style="display: flex; justify-content: space-between">
            <span style="font-weight: 600">引用清单（可定位）</span>
            <span class="pic-muted">
              文档 {{ docCitations.length }} 条 · 指标 {{ metricCitations.length }} 条
            </span>
          </div>
        </template>
        <el-table :data="report.citations" size="small" max-height="360">
          <el-table-column prop="statement_id" label="陈述" width="130" />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">{{ row.statement_type }}</template>
          </el-table-column>
          <el-table-column label="来源" min-width="220">
            <template #default="{ row }">
              {{ row.source_name || row.metric_ref?.metric_name || '—' }}
            </template>
          </el-table-column>
          <el-table-column label="位置" width="150">
            <template #default="{ row }">
              <span v-if="row.page">第 {{ row.page }} 页</span>
              <span v-if="row.document_version"> · {{ row.document_version }}</span>
              <span v-if="row.metric_ref?.dataset_version"> · {{ row.metric_ref.dataset_version }}</span>
            </template>
          </el-table-column>
          <el-table-column label="权限" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.permission_checked ? 'success' : 'danger'">
                {{ row.permission_checked ? '已校验' : '未校验' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <EvidencePanel :evidence="report.evidence" :marks="report.citation_marks" class="pic-section-gap" />
    </template>

    <el-dialog v-model="modifying.visible" title="修改后通过" width="620px">
      <el-alert
        type="info"
        :closable="false"
        :title="'修改内容会与审核人、时间一起写入审核记录（diff）并进审计日志；原有证据与结论不会被删除。'"
        style="margin-bottom: 12px"
      />
      <el-form label-position="top">
        <el-form-item label="修改后的结论摘要（留空表示不改）">
          <el-input v-model="modifying.summary" type="textarea" :rows="4" />
        </el-form-item>
        <el-form-item label="修改后的优先级">
          <el-select v-model="modifying.priority" clearable placeholder="留空表示不改" style="width: 200px">
            <el-option v-for="p in ['P0', 'P1', 'P2', 'P3']" :key="p" :label="p" :value="p" />
          </el-select>
        </el-form-item>
        <el-form-item label="审核意见">
          <el-input v-model="modifying.comment" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="modifying.visible = false">取消</el-button>
        <el-button type="primary" @click="decide('modify')">提交并恢复工作流</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
/**
 * 评测：在界面上触发离线评测并查看历史与分项指标。
 * 注意：评测会真实跑完整工作流；若后端配了真实模型，会按用例数产生费用。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { evaluationApi, systemApi } from '@/api/endpoints'
import type { EvaluationRunOut } from '@/api/types'

const runs = ref<EvaluationRunOut[]>([])
const current = ref<EvaluationRunOut | null>(null)
const cases = ref<Array<{ id: string; category: string; question: string; expect_refusal?: boolean }>>([])
const loading = ref(false)
const running = ref(false)
const limit = ref(8)
const datasetVersion = ref('eval-1.0.0')

const llmEnabled = ref(false)

/** 售后工单评测（需求书 §9）：与诊断评测共用运行历史表，但指标不同 */
const ticketRun = ref<EvaluationRunOut | null>(null)
const ticketRunning = ref(false)
const ticketLimit = ref(8)
const isTicketRun = computed(
  () => String(current.value?.dataset_version ?? '').startsWith('tickets'),
)
const ticketMetrics = computed(() => (ticketRun.value?.metrics ?? {}) as Record<string, unknown>)
/** 评测集的模型与成本信息：点运行之前就要让用户知道"会不会花钱" */
const ticketCaseInfo = ref<{ llm_provider: string; real_model: boolean; estimated_cost_per_case: number }>({
  llm_provider: 'unknown',
  real_model: false,
  estimated_cost_per_case: 0,
})
const ticketFailed = computed(
  () => (ticketRun.value?.details ?? []).filter((item) => !item.passed) as Array<Record<string, unknown>>,
)

async function runTicketEvaluation(): Promise<void> {
  const estimated = ticketCaseInfo.value.estimated_cost_per_case * ticketLimit.value
  if (ticketCaseInfo.value.real_model) {
    // 成本闸门：真实模型下一轮评测要花真金白银，必须先得到明确同意。
    // 后端也会拦（409 real_model_confirmation_required），这里只是把它说清楚。
    try {
      await ElMessageBox.confirm(
        `当前模型为 ${ticketCaseInfo.value.llm_provider}，本次将执行 ${ticketLimit.value} 条真实工单分析，` +
          `预计费用约 ¥${estimated.toFixed(2)}（按实测 ¥${ticketCaseInfo.value.estimated_cost_per_case.toFixed(4)}/例估算）。` +
          '想免费复现离线基线请把后端 LLM_PROVIDER 设为 stub。',
        '这次评测会真实计费',
        { confirmButtonText: '确认并开始', cancelButtonText: '取消', type: 'warning' },
      )
    } catch {
      return
    }
  }
  ticketRunning.value = true
  try {
    const result = await evaluationApi.ticketsRun({
      limit: ticketLimit.value,
      name: `界面售后工单评测 ${new Date().toLocaleString()}`,
      confirm_real_model: true,
    })
    ticketRun.value = result
    ElMessage.success(
      `售后工单评测完成：通过 ${result.metrics?.passed ?? 0}/${result.case_count}`,
    )
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    ticketRunning.value = false
  }
}

const categoryRows = computed(() => {
  const per = (current.value?.metrics?.per_category ?? {}) as Record<string, number>
  return Object.entries(per).map(([category, rate]) => ({ category, rate }))
})

async function load(): Promise<void> {
  loading.value = true
  try {
    runs.value = await evaluationApi.runs(20)
    if (runs.value.length) current.value = runs.value[0]
    const casesResp = await evaluationApi.cases({ limit: 200 })
    cases.value = casesResp.items
    const health = await systemApi.health()
    llmEnabled.value = Boolean((health as { llm?: { enabled?: boolean } }).llm?.enabled)
    const ticketInfo = await evaluationApi.ticketsCases(1)
    ticketCaseInfo.value = {
      llm_provider: ticketInfo.llm_provider,
      real_model: ticketInfo.real_model,
      estimated_cost_per_case: ticketInfo.estimated_cost_per_case,
    }
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

async function run(): Promise<void> {
  running.value = true
  try {
    const payload: Record<string, unknown> = { dataset_version: datasetVersion.value, name: `界面评测 ${new Date().toLocaleString()}` }
    if (limit.value > 0) payload.limit = limit.value
    const result = await evaluationApi.run(payload)
    // 后端没有 failed_count 字段：失败数由逐条结果推导，避免前端猜一个不存在的契约。
    const failed = result.details.filter((item) => !item.passed).length
    ElMessage.success(`评测完成：通过 ${result.case_count - failed}/${result.case_count}`)
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    running.value = false
  }
}

async function openRun(run: EvaluationRunOut): Promise<void> {
  try {
    current.value = await evaluationApi.runs(1).then(() => run)
  } catch {
    current.value = run
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <el-card shadow="never">
      <template #header>
        <span style="font-weight: 600">评测集运行</span>
        <span class="pic-muted" style="margin-left: 10px">
          共 {{ cases.length }} 条用例；当前模型{{ llmEnabled ? '已启用（评测会真实调用，产生费用）' : '为 stub（离线，不产生费用）' }}
        </span>
      </template>

      <el-alert
        v-if="llmEnabled"
        type="warning"
        :closable="false"
        title="注意：后端已配置真实模型，运行评测会按用例数产生实际费用"
        description="一次完整诊断约 ¥0.13；建议先用 limit=8 小样本验证。要跑离线基线请把后端 LLM_PROVIDER 设为 stub。"
        style="margin-bottom: 12px"
      />

      <el-form :inline="true">
        <el-form-item label="用例数">
          <el-input-number v-model="limit" :min="0" :max="50" />
        </el-form-item>
        <el-form-item label="数据集版本">
          <el-input v-model="datasetVersion" style="width: 160px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="running" @click="run">运行评测</el-button>
        </el-form-item>
        <el-form-item>
          <span class="pic-muted">0 表示跑全量 50 条</span>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never" class="pic-section-gap">
      <template #header><span style="font-weight: 600">历史运行</span></template>
      <el-table :data="runs" size="small" @row-click="openRun">
        <el-table-column prop="name" label="名称" min-width="200" />
        <el-table-column prop="dataset_version" label="数据集" width="120" />
        <el-table-column prop="case_count" label="用例数" width="90" />
        <el-table-column label="通过率" width="100">
          <template #default="{ row }">{{ ((row.metrics?.pass_rate ?? 0) * 100).toFixed(0) }}%</template>
        </el-table-column>
        <el-table-column label="拒答准确率" width="110">
          <template #default="{ row }">{{ (row.metrics?.refusal_accuracy ?? 0).toFixed(2) }}</template>
        </el-table-column>
        <el-table-column label="平均耗时" width="110">
          <template #default="{ row }">{{ row.avg_latency_ms }} ms</template>
        </el-table-column>
        <el-table-column prop="created_at" label="时间" min-width="170" />
      </el-table>
    </el-card>

    <el-card shadow="never" class="pic-section-gap">
      <template #header>
        <span style="font-weight: 600">售后工单评测（需求书 §9）</span>
        <span class="pic-muted" style="margin-left: 10px">
          分类 Macro-F1 · 审批门禁召回 · 失效政策泄漏率 · 知识库隔离率 · P95 · 每单成本
        </span>
      </template>
      <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
        <el-input-number v-model="ticketLimit" aria-label="售后评测用例数" :min="1" :max="64" size="small" style="width: 130px" />
        <el-button type="primary" :loading="ticketRunning" @click="runTicketEvaluation">
          运行售后工单评测
        </el-button>
        <span class="pic-muted">
          用例集：64 条（常规 20 / 退款 5 / 风控 3 / 缺单号 4 / 订单不存在 3 / 接口超时 3 / 越权 3 …）
        </span>
      </div>
      <!-- 模型与成本提示：不留"点了才知道要花钱"的盲盒按钮 -->
      <div class="pic-muted" style="margin-top: 6px">
        当前模型：{{ ticketCaseInfo.llm_provider }}
        <template v-if="ticketCaseInfo.real_model">
          · 会真实计费，预计 ¥{{
            (ticketCaseInfo.estimated_cost_per_case * ticketLimit).toFixed(2)
          }}
          （¥{{ ticketCaseInfo.estimated_cost_per_case.toFixed(4) }}/例 × {{ ticketLimit }} 条）
        </template>
        <template v-else>· 零成本（stub 模型，可反复跑作回归基线）</template>
      </div>

      <el-row v-if="ticketRun" :gutter="16" style="margin-top: 14px">
        <el-col :span="4">
          <div class="pic-muted">用例通过率</div>
          <div style="font-size: 20px; font-weight: 600">
            {{ ((Number(ticketMetrics.pass_rate ?? 0)) * 100).toFixed(1) }}%
          </div>
        </el-col>
        <el-col :span="4">
          <div class="pic-muted">分类 Macro-F1</div>
          <div style="font-size: 20px; font-weight: 600">{{ ticketMetrics.issue_macro_f1 ?? '—' }}</div>
        </el-col>
        <el-col :span="4">
          <div class="pic-muted">审批门禁召回</div>
          <div style="font-size: 20px; font-weight: 600">
            {{ ticketMetrics.approval_gate_recall ?? '—' }}
          </div>
        </el-col>
        <el-col :span="4">
          <div class="pic-muted">失效政策泄漏率</div>
          <div style="font-size: 20px; font-weight: 600">
            {{ ticketMetrics.expired_policy_leak_rate ?? '—' }}
          </div>
        </el-col>
        <el-col :span="4">
          <div class="pic-muted">知识库隔离率</div>
          <div style="font-size: 20px; font-weight: 600">
            {{ ticketMetrics.tenant_isolation_rate ?? '—' }}
          </div>
        </el-col>
        <el-col :span="4">
          <div class="pic-muted">端到端 P95</div>
          <div style="font-size: 20px; font-weight: 600">{{ ticketMetrics.p95_latency_ms ?? '—' }} ms</div>
        </el-col>
      </el-row>
      <div v-if="ticketRun" class="pic-muted" style="margin-top: 8px">
        单均成本 {{ ticketMetrics.avg_cost_amount ?? 0 }} {{ ticketMetrics.currency }} · 单均 token
        {{ ticketMetrics.avg_tokens ?? 0 }} · 证据支持率 {{ ticketMetrics.evidence_support_rate ?? '—' }} ·
        {{ ticketMetrics.note }}
      </div>

      <el-table
        v-if="ticketRun && ticketFailed.length"
        :data="ticketFailed"
        size="small"
        style="margin-top: 12px"
      >
        <el-table-column prop="group" label="分组" width="140" />
        <el-table-column prop="case_id" label="用例" width="130" />
        <el-table-column prop="subject" label="工单摘要" min-width="220" show-overflow-tooltip />
        <el-table-column label="失败项" min-width="220">
          <template #default="{ row }">
            {{ Object.keys(row.checks ?? {}).filter((k) => !row.checks[k]).join('、') || row.error }}
          </template>
        </el-table-column>
      </el-table>
      <div v-else-if="ticketRun" class="pic-muted" style="margin-top: 8px">
        未通过用例：无
      </div>
    </el-card>

    <el-card v-if="current && !isTicketRun" shadow="never" class="pic-section-gap">
      <template #header>
        <span style="font-weight: 600">运行详情</span>
        <span class="pic-muted" style="margin-left: 10px">
          {{ current.name }} · workflow {{ current.workflow_version }} · prompt {{ current.prompt_version }}
        </span>
      </template>

      <el-row :gutter="16">
        <el-col :span="6">
          <div class="pic-muted">通过率</div>
          <div style="font-size: 20px; font-weight: 600">{{ ((current.metrics?.pass_rate ?? 0) * 100).toFixed(0) }}%</div>
        </el-col>
        <el-col :span="6">
          <div class="pic-muted">冲突检出率</div>
          <div style="font-size: 20px; font-weight: 600">{{ (current.metrics?.conflict_detection_rate ?? 0).toFixed(2) }}</div>
        </el-col>
        <el-col :span="6">
          <div class="pic-muted">注入处理率</div>
          <div style="font-size: 20px; font-weight: 600">{{ (current.metrics?.injection_handling_rate ?? 0).toFixed(2) }}</div>
        </el-col>
        <el-col :span="6">
          <div class="pic-muted">人工接管率</div>
          <div style="font-size: 20px; font-weight: 600">{{ (current.metrics?.human_takeover_rate ?? 0).toFixed(2) }}</div>
        </el-col>
      </el-row>

      <el-table :data="categoryRows" size="small" style="margin-top: 12px">
        <el-table-column prop="category" label="类别" width="220" />
        <el-table-column label="通过率">
          <template #default="{ row }">{{ (row.rate * 100).toFixed(0) }}%</template>
        </el-table-column>
      </el-table>

      <el-divider content-position="left">未通过用例（诊断项，不代表隐藏失败）</el-divider>
      <el-table :data="(current.details ?? []).filter((d) => !d.passed)" size="small">
        <el-table-column prop="category" label="类别" width="170" />
        <el-table-column prop="question" label="问题" min-width="280" show-overflow-tooltip />
        <el-table-column label="判定明细" min-width="260">
          <template #default="{ row }">
            <span class="pic-muted">{{ JSON.stringify(row.checks) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

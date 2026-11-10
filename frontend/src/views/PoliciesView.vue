<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { policyApi, userApi } from '@/api/endpoints'
import type {
  AccessPolicyConditions,
  AccessPolicyOut,
  AuthorizationSimulation,
  UserOut,
} from '@/api/types'

const policies = ref<AccessPolicyOut[]>([])
const accounts = ref<UserOut[]>([])
const loading = ref(false)
const simulating = ref(false)
const simulation = ref<AuthorizationSimulation | null>(null)
const keyword = ref('')

const RESOURCE_OPTIONS = [
  { value: 'task_replay', label: '任务历史回放' },
  { value: 'ticket', label: '售后工单' },
  { value: 'knowledge_base', label: '知识库' },
  { value: 'document', label: '文档' },
  { value: 'tool', label: '外部工具' },
  { value: 'metric', label: '单个指标' },
  { value: 'metric_domain', label: '指标域' },
  { value: 'dataset', label: '数据集' },
  { value: 'task', label: '分析任务' },
]

const ACTION_OPTIONS = [
  { value: 'task:replay', label: '查看任务完整回放' },
  { value: 'task:read', label: '查看分析任务' },
  { value: 'ticket:read', label: '查看售后工单' },
  { value: 'ticket:approve', label: '审批售后方案' },
  { value: 'metric:read', label: '读取业务指标' },
  { value: 'policy:read', label: '读取售后政策' },
  { value: 'document:upload', label: '上传文档' },
  { value: 'read', label: '通用只读动作' },
]

const EFFECT_LABELS: Record<string, string> = {
  allow: '允许',
  deny: '拒绝',
  require_approval: '需人工审批',
}

const EFFECT_TAGS: Record<string, 'success' | 'danger' | 'warning' | 'info'> = {
  allow: 'success',
  deny: 'danger',
  require_approval: 'warning',
}

const simForm = ref({
  username: 'reviewer',
  action: 'task:replay',
  resource_type: 'task_replay',
  resource_id: 'task-id',
  risk_tier: '',
  amount: '',
  classification: '',
  tool_name: '',
})

const dialogVisible = ref(false)
const submitting = ref(false)
const form = ref({
  subject_type: 'role',
  subject_id: 'reviewer',
  resource_type: 'task_replay',
  resource_id: '*',
  effect: 'require_approval',
  action: 'task:replay',
  conditionsText: '{"risk_tiers": ["high", "critical"]}',
})

const TEMPLATES = [
  {
    label: '高风险任务回放需要人工审批',
    value: {
      subject_type: 'role', subject_id: 'reviewer', resource_type: 'task_replay',
      resource_id: '*', effect: 'require_approval', action: 'task:replay',
      conditionsText: '{"risk_tiers": ["high", "critical"]}',
    },
  },
  {
    label: '售后部门可查看工单',
    value: {
      subject_type: 'role', subject_id: 'operation', resource_type: 'ticket',
      resource_id: '*', effect: 'allow', action: 'ticket:read',
      conditionsText: '{"departments": ["after_sales"]}',
    },
  },
  {
    label: '工作时间内读取指标',
    value: {
      subject_type: 'role', subject_id: 'product_manager', resource_type: 'metric',
      resource_id: '*', effect: 'allow', action: 'metric:read',
      conditionsText: '{"time_window": {"start": "09:00", "end": "18:00", "days": [1,2,3,4,5], "timezone": "+08:00"}}',
    },
  },
  {
    label: '禁止调用指定外部工具',
    value: {
      subject_type: 'role', subject_id: '*', resource_type: 'tool',
      resource_id: '*', effect: 'deny', action: 'read',
      conditionsText: '{"tool_names": ["refund_write"]}',
    },
  },
]

const filtered = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  if (!text) return policies.value
  return policies.value.filter((item) =>
    [item.subject_type, item.subject_id, item.resource_type, item.resource_id, item.effect, ...item.actions]
      .join(' ')
      .toLowerCase()
      .includes(text),
  )
})

function resourceLabel(value: string): string {
  return RESOURCE_OPTIONS.find((item) => item.value === value)?.label || value
}

function actionLabel(value: string): string {
  return ACTION_OPTIONS.find((item) => item.value === value)?.label || value
}

function conditionText(item: AccessPolicyOut): string {
  const c = item.conditions || {}
  const parts: string[] = []
  if (c.min_clearance !== undefined) parts.push(`访问者密级至少 ${c.min_clearance}`)
  if (c.departments?.length) parts.push(`仅限部门：${c.departments.join('、')}`)
  if (c.risk_tiers?.length) parts.push(`风险等级：${c.risk_tiers.join('、')}`)
  if (c.classifications?.length) parts.push(`数据分类：${c.classifications.join('、')}`)
  if (c.tool_names?.length) parts.push(`工具：${c.tool_names.join('、')}`)
  if (c.min_amount !== undefined || c.max_amount !== undefined) {
    parts.push(`金额范围：${c.min_amount ?? '不限'} ～ ${c.max_amount ?? '不限'}`)
  }
  if (c.valid_from) parts.push(`${c.valid_from} 起生效`)
  if (c.valid_until) parts.push(`${c.valid_until} 到期`)
  if (c.time_window) {
    parts.push(`可用时间：${c.time_window.start || '全天开始'} ～ ${c.time_window.end || '全天结束'}`)
  }
  if (c.row_filters) {
    parts.push(Object.entries(c.row_filters).map(([key, values]) => `${key}=${values.join('/')}`).join('；'))
  }
  return parts.join('；') || '无附加条件'
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [policyRows, users] = await Promise.all([policyApi.list(), userApi.list()])
    policies.value = policyRows
    accounts.value = users
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

function applyTemplate(value: (typeof TEMPLATES)[number]['value']): void {
  form.value = { ...value }
}

function simulationContext(): Record<string, unknown> {
  const context: Record<string, unknown> = {}
  if (simForm.value.risk_tier) context.risk_tier = simForm.value.risk_tier
  if (simForm.value.classification) context.classification = simForm.value.classification
  if (simForm.value.tool_name) context.tool_name = simForm.value.tool_name
  if (simForm.value.amount.trim()) {
    const amount = Number(simForm.value.amount)
    if (!Number.isFinite(amount)) throw new Error('金额必须是数字')
    context.amount = amount
  }
  return context
}

async function runSimulation(): Promise<void> {
  if (!simForm.value.username || !simForm.value.resource_id.trim() || !simForm.value.action) {
    ElMessage.warning('请选择账号、动作并填写资源标识')
    return
  }
  simulating.value = true
  try {
    simulation.value = await policyApi.authorizeSimulate({
      username: simForm.value.username,
      action: simForm.value.action,
      resource_type: simForm.value.resource_type,
      resource_id: simForm.value.resource_id.trim(),
      context: simulationContext(),
    })
  } catch (error) {
    simulation.value = null
    ElMessage.error((error as Error).message)
  } finally {
    simulating.value = false
  }
}

function simulateFromPolicy(item: AccessPolicyOut): void {
  simForm.value.resource_type = item.resource_type
  simForm.value.resource_id = item.resource_id === '*' ? '示例资源' : item.resource_id
  simForm.value.action = item.actions[0] || 'read'
  if (item.subject_type === 'user') {
    const user = accounts.value.find((candidate) => candidate.id === item.subject_id)
    if (user) simForm.value.username = user.username
  } else if (item.subject_type === 'role') {
    const user = accounts.value.find((candidate) => candidate.roles.includes(item.subject_id))
    if (user) simForm.value.username = user.username
  }
  simForm.value.risk_tier = item.conditions.risk_tiers?.[0] || ''
  simForm.value.classification = item.conditions.classifications?.[0] || ''
  simForm.value.tool_name = item.conditions.tool_names?.[0] || ''
  void runSimulation()
}

async function submit(): Promise<void> {
  let conditions: AccessPolicyConditions = {}
  try {
    conditions = form.value.conditionsText.trim() ? JSON.parse(form.value.conditionsText) : {}
  } catch {
    ElMessage.error('附加条件不是合法 JSON')
    return
  }
  if (!form.value.subject_id.trim() || !form.value.resource_id.trim() || !form.value.action) {
    ElMessage.warning('请完整填写访问者、资源和动作')
    return
  }
  submitting.value = true
  try {
    await policyApi.create({
      subject_type: form.value.subject_type,
      subject_id: form.value.subject_id.trim(),
      resource_type: form.value.resource_type,
      resource_id: form.value.resource_id.trim(),
      effect: form.value.effect,
      actions: [form.value.action],
      conditions,
    })
    ElMessage.success('访问策略已创建并立即生效')
    dialogVisible.value = false
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    submitting.value = false
  }
}

async function remove(item: AccessPolicyOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除“${item.subject_id} 对 ${resourceLabel(item.resource_type)} ${item.resource_id}”的策略？`,
      '删除访问策略',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await policyApi.remove(item.id)
    ElMessage.success('策略已删除')
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

onMounted(() => void load())
</script>

<template>
  <div v-loading="loading" class="access-policy-page">
    <section class="page-intro">
      <div>
        <h2>访问策略</h2>
        <p>统一控制“谁可以对什么资源做什么”，并在真实操作前预演最终结论。</p>
      </div>
      <el-button type="primary" @click="dialogVisible = true">新增策略</el-button>
    </section>

    <div class="value-strip">
      <div><b>1. 角色权限</b><span>先检查岗位是否具备这个动作</span></div>
      <div><b>2. 资源条件</b><span>再检查租户、风险、金额、时间等限制</span></div>
      <div><b>3. 最终结论</b><span>允许、拒绝，或转人工审批</span></div>
    </div>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <div><b>已配置策略</b><span class="pic-muted">共 {{ policies.length }} 条</span></div>
          <el-input v-model="keyword" clearable placeholder="搜索访问者、资源或动作" style="width: 280px" />
        </div>
      </template>

      <el-table :data="filtered" size="small">
        <el-table-column label="谁" width="170">
          <template #default="{ row }">
            {{ row.subject_id === '*' ? '所有访问者' : row.subject_id }}
            <div class="pic-muted">{{ row.subject_type === 'user' ? '指定账号' : '角色' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="对什么资源" min-width="220">
          <template #default="{ row }">
            {{ resourceLabel(row.resource_type) }}
            <div class="pic-muted">{{ row.resource_id }}</div>
          </template>
        </el-table-column>
        <el-table-column label="做什么" min-width="180">
          <template #default="{ row }">{{ row.actions.map(actionLabel).join('、') }}</template>
        </el-table-column>
        <el-table-column label="系统处理" width="130">
          <template #default="{ row }">
            <el-tag :type="EFFECT_TAGS[row.effect] || 'info'">{{ EFFECT_LABELS[row.effect] || row.effect }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="什么时候生效" min-width="280">
          <template #default="{ row }"><span class="pic-muted">{{ conditionText(row) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="125" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="simulateFromPolicy(row)">预演</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" class="pic-section-gap">
      <template #header><b>授权预演</b><span class="pic-muted">不执行真实操作，只使用和业务接口相同的授权判定</span></template>

      <el-form label-position="top" class="simulation-grid">
        <el-form-item label="访问者账号">
          <el-select v-model="simForm.username" filterable>
            <el-option v-for="item in accounts" :key="item.id" :label="`${item.username}（${item.roles.join('/')}）`" :value="item.username" />
          </el-select>
        </el-form-item>
        <el-form-item label="准备做什么">
          <el-select v-model="simForm.action" filterable allow-create>
            <el-option v-for="item in ACTION_OPTIONS" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="资源类型">
          <el-select v-model="simForm.resource_type">
            <el-option v-for="item in RESOURCE_OPTIONS" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="资源标识">
          <el-input v-model="simForm.resource_id" placeholder="例如任务 ID、工单 ID、指标名" />
        </el-form-item>
      </el-form>

      <el-collapse>
        <el-collapse-item title="补充业务条件（风险、金额、数据分类、工具名）" name="context">
          <div class="context-grid">
            <el-input v-model="simForm.risk_tier" placeholder="风险等级，如 high" />
            <el-input v-model="simForm.amount" placeholder="金额，如 500" />
            <el-input v-model="simForm.classification" placeholder="数据分类，如 confidential" />
            <el-input v-model="simForm.tool_name" placeholder="工具名，如 refund_write" />
          </div>
        </el-collapse-item>
      </el-collapse>

      <el-button type="primary" :loading="simulating" style="margin-top: 14px" @click="runSimulation">查看系统会怎么判定</el-button>

      <div v-if="simulation" class="decision-panel" :class="`decision-${simulation.decision}`">
        <div class="decision-title">
          <el-tag :type="EFFECT_TAGS[simulation.decision] || 'info'" size="large">
            {{ EFFECT_LABELS[simulation.decision] || simulation.decision }}
          </el-tag>
          <b>{{ simulation.subject.username }} {{ actionLabel(simulation.action) }} {{ resourceLabel(simulation.resource.type) }}</b>
        </div>
        <p>{{ simulation.reason }}</p>
        <div class="decision-meta">
          <span>角色：{{ simulation.subject.roles.join('、') || '无' }}</span>
          <span>部门：{{ simulation.subject.department || '未设置' }}</span>
          <span>命中策略：{{ simulation.matched_policy_ids.length ? simulation.matched_policy_ids.length + ' 条' : '0 条' }}</span>
        </div>
        <el-collapse v-if="simulation.matched_policy_ids.length || Object.keys(simulation.effective_scope).length">
          <el-collapse-item title="查看判定依据" name="evidence">
            <div v-if="simulation.matched_policy_ids.length">策略 ID：{{ simulation.matched_policy_ids.join('、') }}</div>
            <pre v-if="Object.keys(simulation.effective_scope).length">{{ JSON.stringify(simulation.effective_scope, null, 2) }}</pre>
          </el-collapse-item>
        </el-collapse>
      </div>
    </el-card>

    <el-dialog v-model="dialogVisible" title="新增访问策略" width="680px">
      <el-form label-width="110px">
        <el-form-item label="常用模板">
          <el-select placeholder="选择后自动填充，可继续修改" style="width: 100%" @change="applyTemplate">
            <el-option v-for="item in TEMPLATES" :key="item.label" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="访问者">
          <el-radio-group v-model="form.subject_type"><el-radio-button label="role">角色</el-radio-button><el-radio-button label="user">指定账号 ID</el-radio-button></el-radio-group>
          <el-input v-model="form.subject_id" style="margin-top: 8px" placeholder="角色码、账号 ID；* 表示所有访问者" />
        </el-form-item>
        <el-form-item label="资源">
          <el-select v-model="form.resource_type" style="width: 210px">
            <el-option v-for="item in RESOURCE_OPTIONS" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
          <el-input v-model="form.resource_id" style="width: calc(100% - 220px); margin-left: 10px" placeholder="资源 ID；* 表示全部" />
        </el-form-item>
        <el-form-item label="动作">
          <el-select v-model="form.action" filterable allow-create style="width: 100%">
            <el-option v-for="item in ACTION_OPTIONS" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="处理方式">
          <el-radio-group v-model="form.effect">
            <el-radio-button label="allow">允许</el-radio-button>
            <el-radio-button label="deny">拒绝</el-radio-button>
            <el-radio-button label="require_approval">需人工审批</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="附加条件">
          <el-input v-model="form.conditionsText" type="textarea" :rows="5" placeholder='可留空；例如 {"risk_tiers":["high"]}' />
          <div class="pic-muted condition-help">
            常用条件：访问者部门 departments、风险等级 risk_tiers、金额 min_amount/max_amount、
            生效日期 valid_from/valid_until、工作时间 time_window。错误条件会被后端拒绝，不会静默忽略。
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">创建并立即生效</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.access-policy-page { display: grid; gap: 16px; }
.page-intro, .card-header, .decision-title { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.page-intro h2 { margin: 0 0 6px; font-size: 22px; }
.page-intro p { margin: 0; color: var(--el-text-color-secondary); }
.value-strip { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; background: var(--el-border-color-light); border: 1px solid var(--el-border-color-light); }
.value-strip > div { display: grid; gap: 4px; padding: 14px 16px; background: var(--el-bg-color); }
.value-strip span, .card-header span { color: var(--el-text-color-secondary); font-size: 13px; }
.card-header > div { display: flex; align-items: baseline; gap: 10px; }
.simulation-grid { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 12px; }
.simulation-grid :deep(.el-form-item) { margin-bottom: 0; }
.simulation-grid :deep(.el-select) { width: 100%; }
.context-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.decision-panel { margin-top: 16px; padding: 16px; border: 1px solid var(--el-border-color); border-left-width: 4px; }
.decision-allow { border-left-color: var(--el-color-success); }
.decision-deny { border-left-color: var(--el-color-danger); }
.decision-require_approval { border-left-color: var(--el-color-warning); }
.decision-title { justify-content: flex-start; }
.decision-panel p { margin: 12px 0; }
.decision-meta { display: flex; flex-wrap: wrap; gap: 18px; color: var(--el-text-color-secondary); font-size: 13px; }
.decision-panel pre { white-space: pre-wrap; margin: 8px 0 0; }
.condition-help { margin-top: 6px; line-height: 1.6; }
@media (max-width: 1000px) {
  .simulation-grid, .context-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 680px) {
  .value-strip, .simulation-grid, .context-grid { grid-template-columns: 1fr; }
  .page-intro { align-items: flex-start; }
}
</style>

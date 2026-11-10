<script setup lang="ts">
/** 工作台：任务列表、运行状态、待审核任务和失败任务。 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import StatusTag from '@/components/StatusTag.vue'
import RiskTag from '@/components/RiskTag.vue'
import { taskApi, systemApi } from '@/api/endpoints'
import type { CanaryExperiment, CostSummary, TaskOut } from '@/api/types'
import { useSessionStore } from '@/stores/session'

const router = useRouter()
const session = useSessionStore()

const tasks = ref<TaskOut[]>([])
const total = ref(0)
const loading = ref(false)
const summary = ref<{ counts: Record<string, number>; pending_review: number; failed: number; running: number } | null>(null)
// 阶段六：成本与缓存可观测性
const todayCost = ref<CostSummary | null>(null)
const cacheInfo = ref<{
  backend: string
  hit_rate: number
  recentRate: number
  recentWindow: number
  layers: string[]
} | null>(null)
// 灰度效果对比：只有真的有两侧样本时才展示，避免平时堆一块"未开启"的噪声
const canary = ref<CanaryExperiment | null>(null)

const canaryRows = computed(() => {
  if (!canary.value) return []
  return [...canary.value.metrics, ...canary.value.report_metrics].map((item) => ({
    ...item,
    // 比率类用百分点（对照为 0 时相对百分比没有意义），均值类用相对百分比
    deltaText:
      item.test === 'two-proportion-z'
        ? `${(item.delta_pp ?? 0).toFixed(1)}pp`
        : `${item.delta_pct.toFixed(1)}%`,
  }))
})

const canaryVisible = computed(() => {
  if (!canary.value) return false
  const { canary: left, control: right } = canary.value.groups
  return left.task_count + right.task_count > 0
})

const canaryVerdictType = computed(() => {
  const verdict = canary.value?.verdict ?? ''
  if (verdict === '灰度更优') return 'success'
  if (verdict === '灰度更差') return 'danger'
  if (verdict === '样本不足') return 'info'
  return 'warning'
})

const filters = ref({ status: '', mine: false, keyword: '', page: 1, page_size: 10 })

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'pending', label: '排队中' },
  { value: 'running', label: '执行中' },
  { value: 'waiting_human', label: '等待人工审核' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

const canCreate = computed(() => session.can('task:create'))

let timer: number | undefined

async function load(): Promise<void> {
  loading.value = true
  try {
    const [list, stats] = await Promise.all([
      taskApi.list({
        status: filters.value.status || undefined,
        mine: filters.value.mine || undefined,
        keyword: filters.value.keyword || undefined,
        page: filters.value.page,
        page_size: filters.value.page_size,
      }),
      taskApi.summary(),
    ])
    tasks.value = list.items
    total.value = list.meta.total
    summary.value = stats
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

/** 成本与缓存状态：失败不影响任务列表展示。 */
async function loadOpsInfo(): Promise<void> {
  try {
    const [costs, health] = await Promise.all([systemApi.costs(7), systemApi.health()])
    todayCost.value = costs
    const cache = health.cache
    cacheInfo.value = cache
      ? {
          backend: cache.backend,
          hit_rate: cache.hit_rate,
          // 累计值会被冷启动拖累，最近窗口才反映"现在管不管用"
          recentRate: cache.recent?.hit_rate ?? cache.hit_rate,
          recentWindow: cache.recent?.window ?? 0,
          layers: Object.entries(cache.namespaces ?? {})
            .sort((a, b) => b[1].total_lookups - a[1].total_lookups)
            .map(([name, bucket]) => `${name}：${(bucket.hit_rate * 100).toFixed(0)}%（${bucket.hits}/${bucket.total_lookups}）`),
        }
      : null
  } catch {
    todayCost.value = null
    cacheInfo.value = null
  }
  // 灰度对比单独 try：这一项失败不应该影响成本/缓存信息的展示
  try {
    canary.value = await systemApi.canaryExperiment(14)
  } catch {
    canary.value = null
  }
}

async function cancel(task: TaskOut): Promise<void> {
  try {
    await ElMessageBox.confirm('取消后工作流会在当前节点边界停止，确认取消？', '取消任务', {
      type: 'warning',
    })
    await taskApi.cancel(task.id, task.state_revision)
    ElMessage.success('已提交取消请求')
    await load()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error((error as Error).message)
  }
}

async function retry(task: TaskOut): Promise<void> {
  try {
    await taskApi.retry(task.id, '工作台手动重试', task.state_revision)
    ElMessage.success('已重新提交执行')
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

function openProgress(task: TaskOut): void {
  void router.push({ name: 'task-progress', params: { id: task.id } })
}

function openReport(task: TaskOut): void {
  void router.push({ name: 'report', params: { id: task.id } })
}

onMounted(async () => {
  await load()
  await loadOpsInfo()
  // 有运行中的任务时自动刷新，避免用户手动点刷新
  timer = window.setInterval(() => {
    if (tasks.value.some((t) => t.status === 'running' || t.status === 'pending')) void load()
    else void taskApi.summary().then((s) => (summary.value = s)).catch(() => undefined)
  }, 5000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div>
    <div class="pic-page-title">
      <h2>工作台</h2>
      <div style="display: flex; gap: 8px">
        <el-button :icon="'Refresh'" @click="load">刷新</el-button>
        <el-button v-if="canCreate" type="primary" @click="router.push({ name: 'task-create' })">
          新建诊断
        </el-button>
      </div>
    </div>

    <div class="pic-stat-grid">
      <el-card shadow="never">
        <div class="pic-muted">执行中</div>
        <div style="font-size: 24px; font-weight: 600">{{ summary?.running ?? 0 }}</div>
      </el-card>
      <el-card shadow="never">
        <div class="pic-muted">待人工审核</div>
        <div style="font-size: 24px; font-weight: 600; color: #e6a23c">
          {{ summary?.pending_review ?? 0 }}
        </div>
      </el-card>
      <el-card shadow="never">
        <div class="pic-muted">失败任务</div>
        <div style="font-size: 24px; font-weight: 600; color: #f56c6c">{{ summary?.failed ?? 0 }}</div>
      </el-card>
      <el-card shadow="never">
        <div class="pic-muted">已完成</div>
        <div style="font-size: 24px; font-weight: 600; color: #27663d">
          {{ summary?.counts?.completed ?? 0 }}
        </div>
      </el-card>
    </div>

    <el-card shadow="never" class="pic-section-gap">
      <div style="display: flex; gap: 10px; flex-wrap: wrap; align-items: center">
        <el-select v-model="filters.status" aria-label="按状态筛选任务" style="width: 170px" @change="((filters.page = 1), load())">
          <el-option
            v-for="option in STATUS_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
        <el-input
          v-model="filters.keyword"
          aria-label="按问题关键词搜索任务"
          placeholder="按问题关键词搜索"
          style="width: 240px"
          clearable
          @keyup.enter="((filters.page = 1), load())"
        />
        <el-checkbox v-model="filters.mine" @change="((filters.page = 1), load())">只看我创建的</el-checkbox>
        <el-button type="primary" plain @click="((filters.page = 1), load())">查询</el-button>
        <span v-if="cacheInfo" class="pic-muted">
          缓存 {{ cacheInfo.backend }} 命中率
          <el-tooltip placement="top">
            <template #content>
              <div>累计命中率会被冷启动长期拖累：刚重启完跑一个新问题时检索键全是新的。</div>
              <div>最近 {{ cacheInfo.recentWindow }} 次：{{ (cacheInfo.recentRate * 100).toFixed(0) }}%</div>
              <div v-for="layer in cacheInfo.layers" :key="layer">{{ layer }}</div>
              <div>分层明细见「系统状态」页。</div>
            </template>
            <span style="text-decoration: underline dotted">
              {{ (cacheInfo.recentRate * 100).toFixed(0) }}%（最近 {{ cacheInfo.recentWindow }} 次）
            </span>
          </el-tooltip>
          <span class="pic-muted">· 累计 {{ (cacheInfo.hit_rate * 100).toFixed(0) }}%</span>
        </span>
        <!-- 今日成本常显：不限额（limit=0）时也要能看到今天花了多少 -->
        <span v-if="todayCost" class="pic-muted">
          今日成本 {{ todayCost.today.currency || todayCost.cost.currency }}
          {{ todayCost.today.amount.toFixed(4) }}
          <span v-if="todayCost.today.limit > 0">/ 上限 {{ todayCost.today.limit }}</span>
        </span>
        <span v-if="todayCost?.today?.exceeded" style="color: #e6a23c">
          今日成本已超上限，新建任务将被拒绝
        </span>
      </div>

      <el-table
        v-loading="loading"
        :data="tasks"
        class="pic-section-gap"
        row-key="id"
        @row-click="openProgress"
      >
        <el-table-column label="问题" min-width="320">
          <template #default="{ row }">
            <!-- 默认标题就是问题的前 60 字，这种情况只显示一次，避免重复两行 -->
            <div style="font-weight: 500">
              {{ row.title && !row.query.startsWith(row.title) ? row.title : row.query.slice(0, 46) }}
            </div>
            <div class="pic-muted">
              {{ row.query.startsWith(row.title ?? '') && row.title ? row.query.slice(row.title.length).trim() || '（见详情）' : row.query.slice(46, 120) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="170">
          <template #default="{ row }">
            <StatusTag
              :status="row.status"
              :retry-count="row.retry_count"
              :waiting-reason="row.waiting_reason"
            />
          </template>
        </el-table-column>
        <el-table-column label="意图 / 风险" width="190">
          <template #default="{ row }">
            <div>{{ row.intent || '—' }}</div>
            <RiskTag :risk="row.risk_level" />
          </template>
        </el-table-column>
        <el-table-column label="证据" width="80">
          <template #default="{ row }">{{ row.evidence_count || '—' }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openProgress(row)">进度</el-button>
            <el-button
              v-if="row.report_id"
              link
              type="success"
              @click.stop="openReport(row)"
            >
              报告
            </el-button>
            <el-button
              v-if="row.status === 'failed' || row.status === 'cancelled'"
              link
              type="warning"
              @click.stop="retry(row)"
            >
              重试
            </el-button>
            <el-button
              v-if="['pending', 'running', 'waiting_human'].includes(row.status)"
              link
              type="danger"
              @click.stop="cancel(row)"
            >
              取消
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="filters.page"
        v-model:page-size="filters.page_size"
        :total="total"
        :page-sizes="[10, 20, 50]"
        layout="total, prev, pager, next"
        class="pic-section-gap"
        @current-change="load"
        @size-change="load"
      />
    </el-card>

    <!-- 灰度效果对比：只在真的有两侧样本时出现，避免平时堆一块"未开启"的噪声 -->
    <el-card v-if="canaryVisible && canary" shadow="never" class="pic-section-gap">
      <template #header>
        <span style="font-weight: 600">灰度效果对比</span>
        <el-tag :type="canaryVerdictType" size="small" effect="plain" style="margin-left: 10px">
          {{ canary.verdict }}
        </el-tag>
        <span class="pic-muted" style="margin-left: 10px">{{ canary.verdict_reason }}</span>
      </template>

      <div class="pic-muted" style="margin-bottom: 8px">
        近 {{ canary.window_days }} 天：灰度侧 {{ canary.groups.canary.task_count }} 个任务 /
        对照侧 {{ canary.groups.control.task_count }} 个任务（每侧 ≥ {{ canary.min_sample }} 才下结论）；
        模型：{{ canary.groups.canary.models.join('、') || '—' }} vs
        {{ canary.groups.control.models.join('、') || '—' }}
      </div>

      <el-table :data="canaryRows" size="small">
        <el-table-column prop="metric" label="指标" width="210" />
        <el-table-column label="灰度" width="110">
          <template #default="{ row }">{{ row.canary }}</template>
        </el-table-column>
        <el-table-column label="对照" width="110">
          <template #default="{ row }">{{ row.control }}</template>
        </el-table-column>
        <el-table-column label="差异" width="110">
          <template #default="{ row }">{{ row.deltaText }}</template>
        </el-table-column>
        <el-table-column label="结论" width="120">
          <template #default="{ row }">{{ row.verdict }}</template>
        </el-table-column>
        <el-table-column label="依据" min-width="220">
          <template #default="{ row }">
            <span class="pic-muted">{{ row.note }}</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="pic-muted" style="margin-top: 8px">
        口径：{{ canary.method.rates }}；{{ canary.method.means }}。局限：{{ canary.method.limitations[0] }}
      </div>
    </el-card>
  </div>
</template>

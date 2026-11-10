<script setup lang="ts">
/**
 * 指标查询：把 4 个固定能力做成可点界面。
 *
 * 除了"能查数"，这个页面的另一个用途是**直观验证指标级密级**：
 * 用不同账号登录后查同一个指标，会看到「无权访问」的原因为 403 / 数据范围越界，
 * 不需要命令行就能核对权限行为。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { metricApi } from '@/api/endpoints'
import type { MetricDefinitionOut, MetricQueryResponse } from '@/api/types'
import MetricLineChart from '@/components/MetricLineChart.vue'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

const definitions = ref<MetricDefinitionOut[]>([])
const result = ref<MetricQueryResponse | null>(null)
const coverage = ref<Record<string, unknown> | null>(null)
const loading = ref(false)
const errorText = ref('')
const errorCode = ref('')

const form = ref({
  capability: 'get_trend' as 'get_trend' | 'compare_versions' | 'breakdown' | 'detect_anomaly',
  metric_name: 'next_day_retention',
  start_date: '2026-08-01',
  end_date: '2026-08-31',
  platform: 'Android',
  app_version: '3.2.0',
  channel: '',
  device: '',
  baseline_version: '3.1.9',
  target_version: '3.2.0',
  release_date: '2026-08-20',
  dimension: 'device',
  method: 'zscore',
  threshold: 2,
})

const capabilityLabel: Record<string, string> = {
  get_trend: '趋势',
  compare_versions: '版本对比',
  breakdown: '维度分群',
  detect_anomaly: '异常检测',
}

const rows = computed(() => result.value?.rows ?? [])
const series = computed(() =>
  form.value.capability === 'get_trend'
    ? rows.value.map((row) => ({ label: String(row.stat_date), value: Number(row.value) }))
    : [],
)

async function loadDefinitions(): Promise<void> {
  try {
    definitions.value = await metricApi.definitions()
    if (definitions.value.length && !definitions.value.some((item) => item.name === form.value.metric_name)) {
      form.value.metric_name = definitions.value[0].name
    }
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function query(): Promise<void> {
  loading.value = true
  errorText.value = ''
  errorCode.value = ''
  result.value = null
  coverage.value = null
  try {
    const params: Record<string, unknown> = {
      metric_name: form.value.metric_name,
      start_date: form.value.start_date,
      end_date: form.value.end_date,
      platform: form.value.platform || undefined,
    }
    if (form.value.capability === 'get_trend') {
      params.app_version = form.value.app_version || undefined
      params.channel = form.value.channel || undefined
      params.device = form.value.device || undefined
      result.value = await metricApi.trend(params)
    } else if (form.value.capability === 'compare_versions') {
      params.channel = form.value.channel || undefined
      params.baseline_version = form.value.baseline_version
      params.target_version = form.value.target_version
      params.release_date = form.value.release_date || undefined
      result.value = await metricApi.compare(params)
    } else if (form.value.capability === 'breakdown') {
      params.app_version = form.value.app_version || undefined
      params.channel = form.value.channel || undefined
      params.dimension = form.value.dimension
      result.value = await metricApi.breakdown(params)
    } else {
      params.app_version = form.value.app_version || undefined
      params.channel = form.value.channel || undefined
      params.method = form.value.method
      params.threshold = form.value.threshold
      result.value = await metricApi.anomaly(params)
    }
    try {
      coverage.value = (await metricApi.coverage(form.value.metric_name)) as unknown as Record<string, unknown>
    } catch {
      coverage.value = null
    }
  } catch (error) {
    const err = error as Error & { code?: string }
    errorText.value = err.message
    errorCode.value = err.code || ''
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await loadDefinitions()
  await query()
})
</script>

<template>
  <div v-loading="loading">
    <el-card shadow="never">
      <template #header>
        <span style="font-weight: 600">指标查询</span>
        <span class="pic-muted" style="margin-left: 10px">
          当前账号 {{ session.displayName }} 可见 {{ definitions.length }} 个指标（无权限的指标不会出现在下拉里）
        </span>
      </template>

      <el-form :inline="true" label-width="80px">
        <el-form-item label="能力">
          <el-select v-model="form.capability" style="width: 140px">
            <el-option v-for="(label, value) in capabilityLabel" :key="value" :label="label" :value="value" />
          </el-select>
        </el-form-item>
        <el-form-item label="指标">
          <el-select v-model="form.metric_name" style="width: 220px">
            <el-option v-for="item in definitions" :key="item.name" :label="`${item.display_name}（${item.name}）`" :value="item.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="时间">
          <el-date-picker v-model="form.start_date" aria-label="开始日期" type="date" value-format="YYYY-MM-DD" style="width: 150px" />
          <span style="margin: 0 6px">~</span>
          <el-date-picker v-model="form.end_date" aria-label="结束日期" type="date" value-format="YYYY-MM-DD" style="width: 150px" />
        </el-form-item>
        <el-form-item label="平台">
          <el-select v-model="form.platform" clearable style="width: 120px">
            <el-option label="Android" value="Android" />
            <el-option label="iOS" value="iOS" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.capability !== 'compare_versions'" label="版本">
          <el-select v-model="form.app_version" clearable style="width: 120px">
            <el-option label="3.1.9" value="3.1.9" />
            <el-option label="3.2.0" value="3.2.0" />
          </el-select>
        </el-form-item>
        <el-form-item label="渠道">
          <el-select v-model="form.channel" clearable style="width: 130px">
            <el-option label="channel_a" value="channel_a" />
            <el-option label="channel_b" value="channel_b" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.capability === 'get_trend'" label="机型">
          <el-select v-model="form.device" clearable style="width: 130px">
            <el-option label="high_end" value="high_end" />
            <el-option label="low_end" value="low_end" />
          </el-select>
        </el-form-item>
        <template v-if="form.capability === 'compare_versions'">
          <el-form-item label="基线版本">
            <el-input v-model="form.baseline_version" style="width: 110px" />
          </el-form-item>
          <el-form-item label="目标版本">
            <el-input v-model="form.target_version" style="width: 110px" />
          </el-form-item>
          <el-form-item label="发布日期">
          <el-date-picker v-model="form.release_date" aria-label="发布日期" type="date" value-format="YYYY-MM-DD" style="width: 150px" />
          </el-form-item>
        </template>
        <el-form-item v-if="form.capability === 'breakdown'" label="分群维度">
          <el-select v-model="form.dimension" style="width: 130px">
            <el-option label="机型 device" value="device" />
            <el-option label="渠道 channel" value="channel" />
            <el-option label="平台 platform" value="platform" />
            <el-option label="版本 app_version" value="app_version" />
          </el-select>
        </el-form-item>
        <template v-if="form.capability === 'detect_anomaly'">
          <el-form-item label="方法">
            <el-select v-model="form.method" style="width: 120px">
              <el-option label="zscore" value="zscore" />
              <el-option label="iqr" value="iqr" />
            </el-select>
          </el-form-item>
          <el-form-item label="阈值">
            <el-input-number v-model="form.threshold" :step="0.5" :min="0.5" />
          </el-form-item>
        </template>
        <el-form-item>
          <el-button type="primary" @click="query">查询</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-alert
      v-if="errorText"
      class="pic-section-gap"
      type="error"
      :closable="false"
      :title="errorCode ? `查询被拒绝（${errorCode}）` : '查询失败'"
      :description="errorText"
    />

    <el-card v-if="result" shadow="never" class="pic-section-gap">
      <template #header>
        <span style="font-weight: 600">{{ capabilityLabel[result.capability] || result.capability }}结果</span>
        <span class="pic-muted" style="margin-left: 10px">
          {{ result.metric_name }} · {{ rows.length }} 行 · 数据集版本 {{ result.dataset_version || '—' }}
        </span>
      </template>

      <el-alert
        v-for="warning in result.warnings"
        :key="warning"
        type="warning"
        :closable="false"
        :title="warning"
        style="margin-bottom: 8px"
      />

      <MetricLineChart v-if="series.length" :points="series" :title="`${result.metric_name} 趋势`" />
      <el-table :data="rows" size="small" max-height="420" style="margin-top: 12px">
        <el-table-column
          v-for="key in Object.keys(rows[0] || {})"
          :key="key"
          :prop="key"
          :label="key"
          show-overflow-tooltip
        />
      </el-table>

      <el-divider content-position="left">summary</el-divider>
      <pre style="margin: 0; font-size: 12px">{{ JSON.stringify(result.summary, null, 2) }}</pre>
    </el-card>

    <el-card v-if="coverage" shadow="never" class="pic-section-gap">
      <template #header><span style="font-weight: 600">数据覆盖度（先看有没有数据，再解读结果）</span></template>
      <pre style="margin: 0; font-size: 12px">{{ JSON.stringify(coverage, null, 2) }}</pre>
    </el-card>
  </div>
</template>

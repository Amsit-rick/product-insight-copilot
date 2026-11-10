<script setup lang="ts">
/** 风险等级 / 优先级标签。 */
import { computed } from 'vue'

const props = defineProps<{
  risk?: string | null
  priority?: string | null
  confidence?: number | null
}>()

const RISK_LABELS: Record<string, string> = { low: '低风险', medium: '中风险', high: '高风险' }
const RISK_TYPES: Record<string, 'success' | 'warning' | 'danger'> = {
  low: 'success',
  medium: 'warning',
  high: 'danger',
}
const CONFIDENCE_LABEL = computed(() =>
  typeof props.confidence === 'number' ? `置信度 ${(props.confidence * 100).toFixed(0)}%` : '',
)
</script>

<template>
  <span style="display: inline-flex; gap: 6px; align-items: center; flex-wrap: wrap">
    <el-tag v-if="risk" :type="RISK_TYPES[risk] ?? 'info'" size="small" effect="dark">
      {{ RISK_LABELS[risk] ?? risk }}
    </el-tag>
    <el-tag v-if="priority" size="small" type="warning" effect="plain">优先级 {{ priority }}</el-tag>
    <el-tag v-if="CONFIDENCE_LABEL" size="small" effect="plain" type="info">{{ CONFIDENCE_LABEL }}</el-tag>
  </span>
</template>

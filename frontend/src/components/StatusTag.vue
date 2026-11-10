<script setup lang="ts">
/** 任务状态标签：与后端 TaskStatus 枚举一一对应。 */
import { computed } from 'vue'
import type { TaskStatus } from '@/api/types'

const props = defineProps<{
  status: TaskStatus | string
  retryCount?: number
  waitingReason?: string | null
}>()

const LABELS: Record<string, string> = {
  pending: '排队中',
  running: '执行中',
  waiting_human: '等待人工审核',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

const TYPES: Record<string, 'primary' | 'success' | 'warning' | 'danger' | 'info'> = {
  pending: 'info',
  running: 'primary',
  waiting_human: 'warning',
  completed: 'success',
  failed: 'danger',
  cancelled: 'info',
}

const label = computed(() => LABELS[props.status] ?? props.status)
const type = computed(() => TYPES[props.status] ?? 'info')
const spinning = computed(() => props.status === 'running')
</script>

<template>
  <el-tooltip :content="waitingReason || label" :disabled="!waitingReason" placement="top">
    <span>
      <el-tag :type="type" size="small">
        <el-icon v-if="spinning" class="is-loading" style="vertical-align: -2px; margin-right: 3px">
          <Loading />
        </el-icon>
        {{ label }}
        <span v-if="retryCount">（重试 {{ retryCount }} 次）</span>
      </el-tag>
    </span>
  </el-tooltip>
</template>

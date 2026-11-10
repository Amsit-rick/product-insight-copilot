<script setup lang="ts">
/** 审计日志（管理员）：登录、任务、文档、导入、审批、越权拒绝等全量留痕。 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { systemApi } from '@/api/endpoints'

type AuditItem = {
  id: string
  action: string
  actor_id?: string
  resource_type?: string
  resource_id?: string
  outcome: string
  detail?: Record<string, unknown>
  created_at: string
}

const items = ref<AuditItem[]>([])
const total = ref(0)
const loading = ref(false)
const actionFilter = ref('')
const limit = ref(100)

const actionOptions = computed(() => Array.from(new Set(items.value.map((item) => item.action))).sort())

const denied = computed(() => items.value.filter((item) => item.outcome !== 'success'))

async function load(): Promise<void> {
  loading.value = true
  try {
    const payload = await systemApi.auditLogs({ action: actionFilter.value || undefined, limit: limit.value })
    items.value = payload.items
    total.value = payload.total
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <el-card shadow="never">
      <template #header>
        <span style="font-weight: 600">审计日志</span>
        <span class="pic-muted" style="margin-left: 10px">
          共 {{ total }} 条（当前展示 {{ items.length }} 条，其中非成功结果 {{ denied.length }} 条）
        </span>
        <el-button link type="primary" style="float: right" @click="load">刷新</el-button>
      </template>

      <el-form :inline="true">
        <el-form-item label="动作">
          <el-select v-model="actionFilter" clearable placeholder="全部" style="width: 220px" @change="load">
            <el-option v-for="action in actionOptions" :key="action" :label="action" :value="action" />
          </el-select>
        </el-form-item>
        <el-form-item label="条数">
          <el-input-number v-model="limit" :min="10" :max="500" :step="50" />
        </el-form-item>
        <el-form-item>
          <el-button @click="load">查询</el-button>
        </el-form-item>
      </el-form>

      <el-alert
        type="info"
        :closable="false"
        title="人工测试建议"
        description="先用 ops 或 eng 触发一次越权请求（例如向 engineering_kb 上传文档、提交越权问题），再回本页刷新，应能看到对应的拒绝记录。"
      />

      <el-table :data="items" size="small" max-height="620" style="margin-top: 12px">
        <el-table-column prop="created_at" label="时间" width="180" />
        <el-table-column prop="action" label="动作" width="220" />
        <el-table-column label="结果" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.outcome === 'success' ? 'success' : 'danger'">{{ row.outcome }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="resource_type" label="资源类型" width="140" />
        <el-table-column prop="resource_id" label="资源 id" width="200" show-overflow-tooltip />
        <el-table-column label="详情" min-width="260">
          <template #default="{ row }">
            <span class="pic-muted">{{ JSON.stringify(row.detail ?? {}) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

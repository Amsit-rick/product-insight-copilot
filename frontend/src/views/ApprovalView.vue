<script setup lang="ts">
/** 人工审核：待审核列表 + 审核请求详情（冲突双方、覆盖度、Agent 摘要）+ 审核操作。 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import RiskTag from '@/components/RiskTag.vue'
import { approvalApi, reportApi } from '@/api/endpoints'
import type { ApprovalOut, ReportOut } from '@/api/types'
import { useSessionStore } from '@/stores/session'

const router = useRouter()
const session = useSessionStore()

const approvals = ref<ApprovalOut[]>([])
const loading = ref(false)
const statusFilter = ref('pending')
const current = ref<ApprovalOut | null>(null)
const currentReport = ref<ReportOut | null>(null)
const comment = ref('')
const acting = ref(false)
const modify = reactive({ visible: false, summary: '', priority: '', comment: '' })

const canApprove = computed(() => session.can('task:approve'))

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await approvalApi.list(statusFilter.value)
    approvals.value = result.items
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

async function select(row: ApprovalOut): Promise<void> {
  current.value = row
  currentReport.value = null
  comment.value = ''
  try {
    const task = await approvalApi.get(row.id)
    const taskDetail = task.report_id
      ? await reportApi.get(task.report_id).catch(() => null)
      : null
    currentReport.value = taskDetail
  } catch {
    currentReport.value = null
  }
}

async function decide(action: 'approve' | 'reject' | 'modify'): Promise<void> {
  if (!current.value) return
  acting.value = true
  try {
    if (action === 'reject') {
      await ElMessageBox.confirm('驳回后结论与证据仍保留留痕，但不作为决策依据。确认驳回？', '驳回', {
        type: 'warning',
      })
    }
    const modifications =
      action === 'modify'
        ? {
            ...(modify.summary ? { summary: modify.summary } : {}),
            ...(modify.priority ? { priority: modify.priority } : {}),
          }
        : undefined
    await approvalApi.decide(
      current.value.id,
      action,
      (action === 'modify' ? modify.comment : comment.value) || undefined,
      modifications,
      current.value.task_state_revision ?? undefined,
    )
    ElMessage.success('审核结论已提交，工作流正在恢复')
    modify.visible = false
    current.value = null
    currentReport.value = null
    await load()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error((error as Error).message)
  } finally {
    acting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="pic-page-title">
      <h2>人工审核</h2>
      <div style="display: flex; gap: 8px; align-items: center">
        <el-select v-model="statusFilter" aria-label="按审核状态筛选" style="width: 150px" @change="load">
          <el-option label="待审核" value="pending" />
          <el-option label="已通过" value="approved" />
          <el-option label="已驳回" value="rejected" />
          <el-option label="已修改" value="modified" />
        </el-select>
        <el-button @click="load">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="!canApprove"
      type="warning"
      :closable="false"
      title="当前角色没有 task:approve 权限，只能查看审核记录。"
      class="pic-section-gap"
    />

    <el-row :gutter="16" class="pic-section-gap">
      <el-col :span="10">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between">
              <span style="font-weight: 600">审核记录</span>
              <span class="pic-muted">{{ approvals.length }} 条</span>
            </div>
          </template>

          <el-empty v-if="!approvals.length && !loading" description="没有待处理的审核" :image-size="60" />

          <div
            v-for="row in approvals"
            :key="row.id"
            class="pic-approval-item"
            :class="{ 'is-active': current?.id === row.id }"
            @click="select(row)"
          >
            <div style="display: flex; justify-content: space-between; gap: 8px">
              <strong style="font-weight: 500">{{ row.task_query?.slice(0, 42) || '（无问题描述）' }}</strong>
              <el-tag size="small" :type="row.status === 'pending' ? 'warning' : 'info'">{{ row.status }}</el-tag>
            </div>
            <div class="pic-muted" style="margin-top: 4px">{{ row.waiting_reason || row.request?.reason }}</div>
            <div style="margin-top: 4px">
              <RiskTag :risk="row.task_risk_level" />
              <span class="pic-muted" style="margin-left: 8px">
                冲突 {{ row.request?.conflicts?.length ?? 0 }} 处 · 证据 {{ row.request?.coverage?.evidence_count ?? 0 }} 条
              </span>
            </div>
          </div>
        </el-card>
      </el-col>

      <el-col :span="14">
        <el-card shadow="never">
          <template #header><span style="font-weight: 600">审核请求详情</span></template>

          <el-empty v-if="!current" description="从左侧选择一条审核记录" :image-size="60" />

          <template v-else>
            <el-descriptions :column="2" size="small" border>
              <el-descriptions-item label="任务">
                <el-button
                  link
                  type="primary"
                  @click="router.push({ name: 'task-progress', params: { id: current.task_id } })"
                >
                  <span class="pic-mono">{{ current.task_id }}</span>
                </el-button>
              </el-descriptions-item>
              <el-descriptions-item label="风险等级">
                <RiskTag :risk="current.task_risk_level" />
              </el-descriptions-item>
              <el-descriptions-item label="升级原因" :span="2">
                {{ current.request?.reason || current.waiting_reason || '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="证据 / 事实">
                {{ current.request?.coverage?.evidence_count ?? 0 }} 条 /
                {{ current.request?.coverage?.fact_count ?? 0 }} 条
              </el-descriptions-item>
              <el-descriptions-item label="指标数据可用">
                {{ current.request?.coverage?.metric_available ? '是' : '否' }}
              </el-descriptions-item>
            </el-descriptions>

            <div v-if="(current.request?.top_facts ?? []).length" class="pic-section-gap">
              <div style="font-weight: 600; margin-bottom: 6px">结论草案（前几条事实）</div>
              <ul style="margin: 0; padding-left: 18px; line-height: 1.8">
                <li v-for="fact in current.request?.top_facts ?? []" :key="fact.id ?? fact.text">
                  {{ fact.text }}
                </li>
              </ul>
            </div>

            <div v-if="(current.request?.conflicts ?? []).length" class="pic-section-gap">
              <div style="font-weight: 600; margin-bottom: 6px">
                证据冲突（{{ current.request?.conflicts?.length }} 处，未合并）
              </div>
              <div v-for="conflict in current.request?.conflicts ?? []" :key="conflict.id" style="margin-bottom: 10px">
                <el-tag type="danger" size="small">{{ conflict.topic }}</el-tag>
                <el-alert
                  type="info"
                  :closable="false"
                  style="margin-top: 6px"
                  :title="`A · ${conflict.statement_a.agent_code}：${conflict.statement_a.text.slice(0, 90)}…`"
                />
                <el-alert
                  type="info"
                  :closable="false"
                  style="margin-top: 4px"
                  :title="`B · ${conflict.statement_b.agent_code}：${conflict.statement_b.text.slice(0, 90)}…`"
                />
              </div>
            </div>

            <div v-if="(current.request?.coverage?.data_gaps ?? []).length" class="pic-section-gap">
              <el-alert
                type="warning"
                :closable="false"
                :title="`数据缺口：${(current.request?.coverage?.data_gaps ?? []).join('；')}`"
              />
            </div>

            <div v-if="currentReport" class="pic-section-gap">
              <el-button link type="primary" @click="router.push({ name: 'report', params: { id: current.task_id } })">
                查看完整报告草案与证据
              </el-button>
            </div>

            <template v-if="canApprove && current.status === 'pending'">
              <el-input
                v-model="comment"
                aria-label="审核意见"
                type="textarea"
                :rows="2"
                placeholder="审核意见（写入审核记录与审计日志）"
                class="pic-section-gap"
              />
              <div style="margin-top: 10px">
                <el-button type="success" :loading="acting" @click="decide('approve')">通过</el-button>
                <el-button type="primary" plain :loading="acting" @click="modify.visible = true">
                  修改后通过
                </el-button>
                <el-button type="danger" plain :loading="acting" @click="decide('reject')">驳回</el-button>
              </div>
            </template>
          </template>
        </el-card>
      </el-col>
    </el-row>

    <el-dialog v-model="modify.visible" title="修改后通过" width="620px">
      <el-form label-position="top">
        <el-form-item label="修改后的结论摘要（留空表示不改）">
          <el-input v-model="modify.summary" type="textarea" :rows="4" />
        </el-form-item>
        <el-form-item label="修改后的优先级">
          <el-select v-model="modify.priority" clearable placeholder="留空表示不改" style="width: 200px">
            <el-option v-for="p in ['P0', 'P1', 'P2', 'P3']" :key="p" :label="p" :value="p" />
          </el-select>
        </el-form-item>
        <el-form-item label="审核意见">
          <el-input v-model="modify.comment" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="modify.visible = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="decide('modify')">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.pic-approval-item {
  padding: 10px 12px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  margin-bottom: 8px;
  cursor: pointer;
  transition: all 0.15s;
}
.pic-approval-item:hover {
  border-color: #c6e2ff;
  background: #f5f9ff;
}
.pic-approval-item.is-active {
  border-color: #409eff;
  background: #ecf5ff;
}
</style>

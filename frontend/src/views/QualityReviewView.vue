<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ticketApi } from '@/api/endpoints'
import type { TicketDetailOut, TicketMessageOut, TicketOut } from '@/api/types'

const tickets = ref<TicketOut[]>([])
const current = ref<TicketDetailOut | null>(null)
const loading = ref(false)
const detailLoading = ref(false)
const submitting = ref(false)
const keyword = ref('')
const qualityFilter = ref<'all' | 'unreviewed' | 'needs_followup' | 'block' | 'pass'>('all')
const issueFilter = ref('all')
const result = ref<'pass' | 'needs_followup' | 'block'>('pass')
const issueCodes = ref<string[]>([])
const comment = ref('')
const page = ref(1)
const pageSize = 20
const total = ref(0)

const issueOptions = [
  { value: 'missing_evidence', label: '事实证据不足' },
  { value: 'wrong_policy', label: '政策依据不匹配' },
  { value: 'unsafe_reply', label: '回复存在承诺风险' },
  { value: 'wrong_action', label: '建议动作不合适' },
  { value: 'unclear_next_step', label: '下一步不清晰' },
]

const PRIORITY_LABELS: Record<string, string> = {
  low: '低',
  medium: '中',
  high: '高',
  urgent: '紧急',
}

const ISSUE_TYPE_LABELS: Record<string, string> = {
  refund: '退款申请',
  return: '退货申请',
  exchange: '换货申请',
  logistics_stalled: '物流停滞',
  missing_item: '商品少件',
  damaged: '商品破损',
  wrong_item: '错发商品',
  not_shipped: '整单未发货',
  promotion_not_applied: '优惠未生效',
  description_mismatch: '商品与描述不符',
  other: '其他问题',
}

const ACTION_LABELS: Record<string, string> = {
  reply_only: '解释说明',
  ask_customer: '向客户补充信息',
  refund: '退款处理',
  return_goods: '退货处理',
  exchange: '换货处理',
  reship: '补发商品',
  compensate: '赔付处理',
  coupon: '优惠券补偿',
  contact_logistics: '联系承运商',
  escalate: '升级处理',
  reject: '驳回诉求',
}

const filteredTickets = computed(() => tickets.value)

const qualityNotes = computed(() =>
  (current.value?.messages ?? []).filter((item) => item.meta?.message_type === 'quality_review'),
)

function resultLabel(value: string | undefined): string {
  return value === 'pass' ? '抽查通过' : value === 'needs_followup' ? '需要补证据' : value === 'block' ? '阻断发送' : '未抽查'
}

function resultType(value: string | undefined): 'success' | 'warning' | 'danger' | 'info' {
  return value === 'pass' ? 'success' : value === 'needs_followup' ? 'warning' : value === 'block' ? 'danger' : 'info'
}

function ticketQualityLabel(value: string | undefined): string {
  return resultLabel(value || 'unreviewed')
}

function ticketQualityType(value: string | undefined): 'success' | 'warning' | 'danger' | 'info' {
  return resultType(value === 'unreviewed' || !value ? undefined : value)
}

function issueLabel(code: string): string {
  return ISSUE_TYPE_LABELS[code] || '其他问题'
}

function businessText(value: string): string {
  return Object.entries({ ...ISSUE_TYPE_LABELS, ...ACTION_LABELS }).reduce(
    (text, [code, label]) => text.replace(new RegExp(`\\b${code}\\b`, 'g'), label),
    value,
  )
}

function priorityLabel(value: string): string {
  return PRIORITY_LABELS[value] || value
}

function actionLabel(value: string): string {
  return ACTION_LABELS[value] || value
}

function latestReview(messages: TicketMessageOut[]): TicketMessageOut | undefined {
  return [...messages].reverse().find((item) => item.meta?.message_type === 'quality_review')
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const response = await ticketApi.qualityReviews({
      page: page.value,
      page_size: pageSize,
      keyword: keyword.value.trim() || undefined,
      quality_status: qualityFilter.value === 'all' ? undefined : qualityFilter.value,
      quality_issue_code: issueFilter.value === 'all' ? undefined : issueFilter.value,
    })
    tickets.value = response.items
    total.value = response.meta.total
    if (!current.value && tickets.value.length) await select(tickets.value[0])
  } catch (error) {
    ElMessage.error(`加载抽查工单失败：${(error as Error).message}`)
  } finally {
    loading.value = false
  }
}

async function changeQualityFilter(value: typeof qualityFilter.value): Promise<void> {
  qualityFilter.value = value
  page.value = 1
  await load()
}

async function changeIssueFilter(value: string): Promise<void> {
  issueFilter.value = value
  page.value = 1
  await load()
}

async function searchKeyword(): Promise<void> {
  page.value = 1
  await load()
}

async function changePage(value: number): Promise<void> {
  page.value = value
  await load()
}

async function exportReviews(): Promise<void> {
  try {
    const csv = await ticketApi.qualityReviewsExport({
      keyword: keyword.value.trim() || undefined,
      quality_status: qualityFilter.value === 'all' ? undefined : qualityFilter.value,
      quality_issue_code: issueFilter.value === 'all' ? undefined : issueFilter.value,
    })
    const count = Math.max(0, csv.trim().split('\n').length - 1)
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = `售后质检抽查-${new Date().toISOString().slice(0, 10)}.csv`
    link.click()
    URL.revokeObjectURL(link.href)
    ElMessage.success(`已导出 ${count} 条筛选结果`)
  } catch (error) {
    ElMessage.error(`导出失败：${(error as Error).message}`)
  }
}

async function select(ticket: TicketOut): Promise<void> {
  detailLoading.value = true
  try {
    current.value = await ticketApi.detail(ticket.id)
    const review = latestReview(current.value.messages)
    result.value = (review?.meta?.quality_result as typeof result.value) || 'pass'
    issueCodes.value = (review?.meta?.quality_issue_codes as string[]) || []
    comment.value = review?.content || ''
  } catch (error) {
    ElMessage.error(`打开工单失败：${(error as Error).message}`)
  } finally {
    detailLoading.value = false
  }
}

async function submitReview(): Promise<void> {
  if (!current.value) return
  if (!comment.value.trim()) {
    ElMessage.warning('请写一句抽查意见，便于客服和主管回看')
    return
  }
  submitting.value = true
  try {
    await ticketApi.reply(current.value.ticket.id, {
      content: comment.value.trim(),
      visible_to_customer: false,
      message_type: 'internal_note',
      quality_review: true,
      quality_result: result.value,
      quality_issue_codes: issueCodes.value,
    })
    ElMessage.success('质检结果已记录，不会发送给客户')
    await select(current.value.ticket)
    await load()
  } catch (error) {
    ElMessage.error(`保存质检结果失败：${(error as Error).message}`)
  } finally {
    submitting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="quality-page">
    <div class="pic-page-title">
      <div>
        <h2>质检抽查</h2>
        <p class="pic-muted">抽查建议和回复是否有事实依据；结果只写入内部记录，不会发送给客户。</p>
      </div>
      <div class="quality-page__actions">
        <el-input v-model="keyword" aria-label="搜索质检工单" clearable placeholder="搜索工单号、订单号或问题" style="width: 250px" @keyup.enter="searchKeyword" @clear="searchKeyword" />
        <el-select :model-value="qualityFilter" aria-label="按质检状态筛选" style="width: 150px" @update:model-value="changeQualityFilter">
          <el-option label="全部状态" value="all" />
          <el-option label="未抽查" value="unreviewed" />
          <el-option label="需补证据" value="needs_followup" />
          <el-option label="阻断发送" value="block" />
          <el-option label="抽查通过" value="pass" />
        </el-select>
        <el-select :model-value="issueFilter" aria-label="按问题标记筛选" style="width: 170px" @update:model-value="changeIssueFilter">
          <el-option label="全部问题标记" value="all" />
          <el-option v-for="item in issueOptions" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-button @click="exportReviews">导出筛选结果</el-button>
        <el-button @click="load">刷新</el-button>
      </div>
    </div>

    <el-row :gutter="16" class="pic-section-gap">
      <el-col :span="9">
        <el-card shadow="never" v-loading="loading">
          <template #header>
            <div class="quality-list-head"><strong>质检工单</strong><span class="pic-muted">当前页 {{ filteredTickets.length }} 条，共 {{ total }} 条</span></div>
          </template>
          <el-empty v-if="!filteredTickets.length && !loading" description="暂时没有可抽查工单" :image-size="64" />
          <button
            v-for="ticket in filteredTickets"
            :key="ticket.id"
            class="quality-ticket"
            :class="{ 'is-active': current?.ticket.id === ticket.id }"
            type="button"
            @click="select(ticket)"
          >
            <span class="quality-ticket__main">
              <strong>{{ ticket.external_id || `#${ticket.id.slice(-8)}` }}</strong>
              <span>{{ ticket.subject }}</span>
            </span>
            <span class="quality-ticket__meta">
              <el-tag size="small" :type="ticket.priority === 'urgent' || ticket.priority === 'high' ? 'danger' : 'info'">{{ priorityLabel(ticket.priority) }}优先级</el-tag>
              <el-tag size="small" :type="ticketQualityType(ticket.quality_status)">{{ ticketQualityLabel(ticket.quality_status) }}</el-tag>
              <span>{{ ticket.handler_label || '未分配' }}</span>
            </span>
          </button>
        </el-card>
      </el-col>

      <el-col :span="15">
        <el-card shadow="never" v-loading="detailLoading">
          <el-empty v-if="!current" description="从左侧选择一张工单开始抽查" :image-size="72" />
          <template v-else>
            <div class="quality-detail-head">
              <div>
                <h3>{{ current.ticket.subject }}</h3>
                <span class="pic-muted">{{ current.ticket.external_id || current.ticket.id }} · {{ current.ticket.order_id || '未提供订单号' }}</span>
                <div class="quality-detail__labels">
                  <el-tag size="small" type="info">{{ issueLabel(current.ticket.issue_type || 'other') }}</el-tag>
                  <el-tag size="small" :type="current.ticket.priority === 'urgent' || current.ticket.priority === 'high' ? 'danger' : 'info'">{{ priorityLabel(current.ticket.priority) }}优先级</el-tag>
                </div>
              </div>
              <el-tag :type="resultType(latestReview(current.messages)?.meta?.quality_result as string | undefined)">
                {{ resultLabel(latestReview(current.messages)?.meta?.quality_result as string | undefined) }}
              </el-tag>
            </div>

            <div class="quality-section">
              <h4>客户问题</h4>
              <div class="quality-quote">{{ current.ticket.content }}</div>
            </div>
            <div class="quality-grid">
              <section>
                <h4>系统建议</h4>
                <div v-if="current.recommendation" class="quality-box">
                  <strong>{{ actionLabel(current.recommendation.action_type) }}</strong>
                  <p>{{ businessText(current.recommendation.rationale || '未提供处理理由') }}</p>
                  <span class="pic-muted">{{ current.recommendation.approval_label || '需按当前状态确认' }}</span>
                </div>
                <el-empty v-else description="尚未生成处理建议" :image-size="50" />
              </section>
              <section>
                <h4>关键事实</h4>
                <ul class="quality-facts">
                  <li v-for="fact in current.fact_rows.slice(0, 5)" :key="fact.key"><span>{{ fact.label }}</span><strong>{{ fact.value || '—' }}</strong></li>
                  <li v-if="!current.fact_rows.length" class="pic-muted">暂无已核实事实</li>
                </ul>
              </section>
            </div>

            <div class="quality-section">
              <h4>本次抽查结论</h4>
              <el-radio-group v-model="result">
                <el-radio-button label="pass">抽查通过</el-radio-button>
                <el-radio-button label="needs_followup">需要补证据</el-radio-button>
                <el-radio-button label="block">阻断发送</el-radio-button>
              </el-radio-group>
              <el-checkbox-group v-model="issueCodes" class="quality-issues">
                <el-checkbox v-for="item in issueOptions" :key="item.value" :label="item.value">{{ item.label }}</el-checkbox>
              </el-checkbox-group>
              <el-input v-model="comment" aria-label="本次质检意见" type="textarea" :rows="3" placeholder="写清楚依据、缺口或需要客服补做的动作" />
              <div class="quality-submit"><el-button type="primary" :loading="submitting" @click="submitReview">保存质检结果</el-button></div>
            </div>

            <div v-if="qualityNotes.length" class="quality-section">
              <h4>历史质检记录</h4>
              <div v-for="note in qualityNotes" :key="note.id" class="quality-note">
                <div><el-tag size="small" :type="resultType(note.meta?.quality_result)">{{ resultLabel(note.meta?.quality_result) }}</el-tag><span class="pic-muted">{{ new Date(note.created_at).toLocaleString() }}</span></div>
                <p>{{ note.content }}</p>
                <span v-if="note.meta?.quality_issue_codes?.length" class="pic-muted">问题：{{ note.meta.quality_issue_codes.map(issueLabel).join('、') }}</span>
              </div>
            </div>
          </template>
          <el-pagination
            v-if="total > pageSize"
            class="quality-pagination"
            layout="prev, pager, next"
            :current-page="page"
            :page-size="pageSize"
            :total="total"
            @current-change="changePage"
          />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.quality-page__actions, .quality-list-head, .quality-detail-head, .quality-submit { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.quality-ticket { display: flex; width: 100%; border: 0; border-bottom: 1px solid #edf0f2; background: transparent; padding: 12px 4px; text-align: left; cursor: pointer; justify-content: space-between; gap: 10px; }
.quality-ticket:hover, .quality-ticket.is-active { background: #f3f7ff; }
.quality-ticket:focus-visible { outline: 2px solid #176b5b; outline-offset: -2px; }
.quality-ticket__main, .quality-ticket__meta { display: flex; flex-direction: column; gap: 5px; }
.quality-ticket__main span { color: #59636d; font-size: 13px; }
.quality-ticket__meta { align-items: flex-end; white-space: nowrap; color: #59636d; font-size: 12px; }
.quality-detail-head { border-bottom: 1px solid #edf0f2; padding-bottom: 12px; }
.quality-detail-head h3 { margin: 0 0 5px; }
.quality-detail__labels { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.quality-section { margin-top: 18px; }
.quality-section h4, .quality-grid h4 { margin: 0 0 8px; }
.quality-quote, .quality-box { border: 1px solid #e7ebef; background: #fafbfc; padding: 12px; border-radius: 6px; line-height: 1.65; }
.quality-box p { margin: 8px 0; }
.quality-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 18px; }
.quality-facts { list-style: none; margin: 0; padding: 0; border: 1px solid #e7ebef; border-radius: 6px; }
.quality-facts li { display: flex; justify-content: space-between; gap: 10px; padding: 8px 10px; border-bottom: 1px solid #edf0f2; font-size: 13px; }
.quality-facts li:last-child { border-bottom: 0; }
.quality-facts span { color: #646b73; }
.quality-issues { display: flex; flex-wrap: wrap; gap: 8px 14px; margin: 12px 0; }
.quality-note { border-top: 1px solid #edf0f2; padding: 10px 0; }
.quality-note p { margin: 7px 0; line-height: 1.6; }
.quality-pagination { justify-content: flex-end; margin-top: 14px; }
@media (max-width: 900px) { .quality-page__actions { align-items: stretch; flex-direction: column; } .quality-page__actions .el-input { width: 100% !important; } .quality-grid { grid-template-columns: 1fr; } }
</style>

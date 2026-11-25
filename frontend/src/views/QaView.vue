<script setup lang="ts">
import { computed, inject, nextTick, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { knowledgeApi, qaApi } from '@/api/endpoints'
import type {
  QaAnswer,
  QaCitation,
  QaConversation,
  QaConversationMessage,
  KnowledgeBaseOut,
} from '@/api/types'
import { useSessionStore } from '@/stores/session'
import { ArrowRight, BookOpen, ChevronRight, Info, LoaderCircle, MessageSquareText, Plus, Search, Send, SlidersHorizontal } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { runtimeHealthKey } from '@/lib/runtime'

const router = useRouter()
const session = useSessionStore()
const runtimeHealth = inject(runtimeHealthKey, ref(null))
const retrievalOnlyMode = computed(() => runtimeHealth.value?.llm.enabled === false)

interface Turn {
  id?: string
  question: string
  answer?: QaAnswer
  error?: string
  errorCode?: string
  loading: boolean
  createdAt?: string
}

const question = ref('')
const historySearch = ref('')
const showSettings = ref(false)
const turns = ref<Turn[]>([])
const conversations = ref<QaConversation[]>([])
const filteredConversations = computed(() => {
  const keyword = historySearch.value.trim().toLocaleLowerCase()
  return conversations.value.filter((item) => item.title.toLocaleLowerCase().includes(keyword))
})
const activeConversationId = ref<string | null>(null)
const knowledgeBases = ref<KnowledgeBaseOut[]>([])
const selectedKbs = ref<string[]>([])
const useModel = ref(true)
const topK = ref(6)
const streamRef = ref<HTMLElement | null>(null)
const questionRef = ref<{ focus: () => void } | null>(null)
const conversationListLoading = ref(false)
const conversationLoading = ref(false)
const conversationError = ref('')

const knowledgeBaseLabels: Record<string, string> = {
  feedback_kb: '用户反馈', operation_kb: '运营资料', policy_kb: '售后政策',
  product_kb: '产品资料', engineering_kb: '研发资料',
}
function knowledgeBaseName(code: string): string {
  const item = knowledgeBases.value.find((kb) => kb.code === code)
  return item?.name && item.name !== code ? item.name : knowledgeBaseLabels[code] || code
}
const availableKnowledgeBases = computed(() => knowledgeBases.value.filter((kb) => kb.chunk_count > 0))
function chooseKnowledgeBase(code: string): void {
  selectedKbs.value = [code]
  questionRef.value?.focus()
}
function isRetrievalResult(answer: QaAnswer): boolean {
  return !answer.insufficient && answer.mode === 'extractive'
    && answer.answer.trim() === '根据知识库中的以下材料（点编号可查看原文片段）：'
}
function displayAnswer(answer: QaAnswer): string {
  if (answer.insufficient && answer.coverage?.answerable !== true) {
    return answer.citations.length
      ? '检索到了一些相关资料，但目前的依据还不足以可靠回答这个问题，暂时不能给出确定结论。你可以补充时间、版本或更具体的主题后重试。'
      : '目前在你可访问的资料中，没有检索到足够的回答依据。可以补充具体主题后重试，或联系知识库维护人补充相关资料。'
  }
  if (isRetrievalResult(answer)) {
    return '已找到相关资料，当前仅提供检索结果，尚未生成归纳答案。可以展开下方资料核对原文。'
  }
  return answer.answer
}

const canCreateTask = computed(() => session.can('task:create'))
const sending = computed(() => turns.value.some((item) => item.loading))
const conversationBusy = computed(
  () => conversationLoading.value || conversationListLoading.value,
)
const scopeSummary = computed(() =>
  selectedKbs.value.length ? selectedKbs.value.map(knowledgeBaseName).join('、') : '全部可见知识库',
)

onMounted(async () => {
  // 进入知识库问答时始终停留在问答首页。历史会话只展示在左侧，
  // 用户明确点击某条记录后才加载内容，避免一进页面就误入最近会话。
  void loadConversations()
  try {
    const list = await knowledgeApi.list()
    knowledgeBases.value = list
  } catch {
    knowledgeBases.value = []
  }
})

function messageTurn(item: QaConversationMessage): Turn {
  return {
    id: item.id,
    question: item.query,
    answer: item.answer,
    loading: false,
    createdAt: item.created_at,
  }
}

async function loadConversations(): Promise<void> {
  if (conversationListLoading.value) return
  conversationListLoading.value = true
  conversationError.value = ''
  try {
    const result = await qaApi.conversations({ page: 1, page_size: 100 })
    conversations.value = result.items
  } catch (error) {
    conversationError.value = (error as Error).message || '会话列表加载失败'
  } finally {
    conversationListLoading.value = false
  }
}

async function openConversation(conversationId: string): Promise<void> {
  if (conversationLoading.value || sending.value) return
  conversationLoading.value = true
  conversationError.value = ''
  try {
    const result = await qaApi.messages(conversationId, { page: 1, page_size: 200 })
    activeConversationId.value = conversationId
    turns.value = result.items.map(messageTurn)
    await scrollToBottom()
  } catch (error) {
    conversationError.value = (error as Error).message || '会话内容加载失败'
  } finally {
    conversationLoading.value = false
  }
}

function newConversation(): void {
  if (sending.value) {
    ElMessage.info('当前问题回答完成后再新建会话')
    return
  }
  activeConversationId.value = null
  turns.value = []
  question.value = ''
  conversationError.value = ''
}

async function scrollToBottom(): Promise<void> {
  await nextTick()
  const element = streamRef.value
  if (element) element.scrollTop = element.scrollHeight
}

async function send(text?: string): Promise<void> {
  if (conversationBusy.value) {
    ElMessage.info('正在加载会话，请稍后再提问')
    return
  }
  const content = (text ?? question.value).trim()
  if (!content) {
    ElMessage.warning('请输入问题')
    return
  }
  question.value = ''
  const turn = reactive<Turn>({
    id: `pending-${Date.now()}-${turns.value.length}`,
    question: content,
    loading: true,
  })
  turns.value.push(turn)
  await scrollToBottom()
  try {
    const answer = await qaApi.ask({
      query: content,
      conversation_id: activeConversationId.value || undefined,
      knowledge_bases: selectedKbs.value,
      top_k: topK.value,
      use_model: useModel.value,
    })
    turn.answer = answer
    turn.id = answer.message_id || turn.id
    turn.createdAt = answer.created_at
    if (answer.conversation_id) activeConversationId.value = answer.conversation_id
    await loadConversations()
  } catch (error) {
    const err = error as Error & { code?: string }
    turn.error = err.message
    turn.errorCode = err.code || ''
  } finally {
    turn.loading = false
    await scrollToBottom()
  }
}

function toDiagnosis(text: string): void {
  void router.push({ name: 'task-create', query: { q: text } })
}

function citationText(item: QaCitation): string {
  return `${item.kb_code} · ${item.source_name}${item.page ? ` · 第 ${item.page} 页` : ''} · 版本 ${item.document_version}`
}
</script>

<template>
  <div class="qa">
    <aside class="qa__conversations" aria-label="问答会话列表">
      <div class="qa__conversations-head">
        <strong><MessageSquareText :size="15" />问答会话</strong>
        <Button size="sm" variant="outline" :disabled="sending || conversationBusy" @click="newConversation"><Plus :size="14" />新建会话</Button>
      </div>
      <div class="qa__history-search"><Search :size="14" aria-hidden="true" /><Input v-model="historySearch" aria-label="搜索已加载的历史会话" placeholder="搜索当前会话列表…" /></div>
      <div v-if="conversationListLoading && !conversations.length" class="qa__conversation-loading">
        <el-skeleton :rows="4" animated />
      </div>
      <div v-else-if="!conversations.length" class="qa__conversation-empty">
        还没有归档会话
      </div>
      <div v-else-if="!filteredConversations.length" class="qa__conversation-empty">没有匹配的会话</div>
      <div v-else class="qa__conversation-list">
        <button
          v-for="item in filteredConversations"
          :key="item.id"
          type="button"
          class="qa__conversation-item"
          :class="{ 'is-active': item.id === activeConversationId }"
          :disabled="conversationLoading || sending"
          @click="openConversation(item.id)"
        >
          <span class="qa__conversation-title"><MessageSquareText :size="13" aria-hidden="true" />{{ item.title }}</span>
          <span class="qa__conversation-meta">
            {{ item.message_count }} 轮 · {{ new Date(item.updated_at).toLocaleString() }}
          </span>
        </button>
      </div>
    </aside>

    <section class="qa__main">
    <!-- 顶栏：检索设置（固定，不随对话滚动） -->
    <div class="qa__head">
      <div class="qa__head-inner">
        <div class="qa__title">
          <div><h1>知识库问答 <Badge v-if="retrievalOnlyMode" variant="outline" data-testid="qa-model-mode">资料检索模式</Badge></h1><span class="pic-muted">{{ retrievalOnlyMode ? '当前未启用答案生成，可检索并核对原始资料。' : '查找资料，核对依据，继续你的每一轮提问。' }}</span></div>
          <Button variant="outline" size="sm" data-testid="qa-settings-toggle" :aria-expanded="showSettings" aria-controls="qa-retrieval-settings" @click="showSettings = !showSettings"><SlidersHorizontal :size="14" />检索设置</Button>
        </div>
        <el-form v-show="showSettings" id="qa-retrieval-settings" :inline="true" size="small" class="qa__settings">
          <p class="pic-muted">本页按资料回答，不作冲突裁决，也不进入人工审核。<template v-if="canCreateTask">需要进一步核验时，可将回答转成完整诊断。</template></p>
          <el-form-item label="检索范围">
            <el-select
              v-model="selectedKbs"
              multiple
              collapse-tags
              clearable
              placeholder="默认全部可见知识库"
              style="width: 260px"
            >
              <el-option v-for="kb in knowledgeBases" :key="kb.code" :label="knowledgeBaseName(kb.code)" :value="kb.code" />
            </el-select>
          </el-form-item>
          <el-form-item label="引用条数">
            <el-input-number v-model="topK" :min="1" :max="20" size="small" style="width: 110px" />
          </el-form-item>
          <el-form-item label="模型归纳">
            <el-switch v-model="useModel" :disabled="retrievalOnlyMode" />
            <span class="pic-muted" style="margin-left: 8px">
              {{ retrievalOnlyMode ? '当前服务未启用生成模型' : useModel ? '有模型时归纳成一段话（引用仍来自片段）' : '纯检索式：只给要点与引用' }}
            </span>
          </el-form-item>
        </el-form>
      </div>
    </div>

    <!-- 对话流：自己滚动 -->
    <div ref="streamRef" class="qa__stream">
      <div class="qa__stream-inner">
        <div v-if="conversationError" class="qa__history-error">
          <span>{{ conversationError }}</span>
          <el-button
            link
            type="primary"
            :loading="conversationLoading || conversationListLoading"
            @click="activeConversationId ? openConversation(activeConversationId) : loadConversations()"
          >
            重试
          </el-button>
        </div>
        <div v-if="!turns.length && conversationLoading" class="qa__welcome">
          <el-skeleton :rows="3" animated />
        </div>
        <div v-else-if="!turns.length" class="qa__welcome">
          <div class="qa__welcome-mark"><BookOpen :size="28" :stroke-width="1.5" aria-hidden="true" /></div>
          <Badge variant="outline" class="qa__welcome-badge">你的企业知识助手</Badge>
          <h2>有什么需要查证的？</h2>
          <p>{{ retrievalOnlyMode ? '从你有权限的资料中查找依据，展开来源核对原文。' : '从你有权限的资料中查找依据，整理成可以直接阅读的回答。' }}</p>
          <div class="qa__suggestions-label">{{ availableKnowledgeBases.length ? '先选择资料范围，再输入你的问题' : '输入具体的主题或问题，按当前可见资料检索' }}</div>
          <div class="qa__examples">
            <button v-for="kb in availableKnowledgeBases" :key="kb.code" type="button" :aria-pressed="selectedKbs.includes(kb.code)" :disabled="sending || conversationBusy" @click="chooseKnowledgeBase(kb.code)"><span><strong>{{ knowledgeBaseName(kb.code) }}</strong><small>已收录 {{ kb.document_count }} 份资料 · 在此范围内提问</small></span><ArrowRight :size="15" aria-hidden="true" /></button>
          </div>
          <div class="qa__welcome-note">回答以现有资料为依据；证据不足时会明确提示。</div>
        </div>

        <el-card v-for="(turn, index) in turns" :key="turn.id || index" shadow="never" class="qa__turn">
          <template #header>
            <div class="qa__ask">
              <el-tag size="small" type="info" effect="plain">问</el-tag>
              <span class="qa__ask-text">{{ turn.question }}</span>
              <span v-if="turn.createdAt" class="qa__timestamp">{{ new Date(turn.createdAt).toLocaleString() }}</span>
            </div>
          </template>

          <el-skeleton v-if="turn.loading" :rows="3" animated />

          <el-alert
            v-else-if="turn.error"
            type="error"
            :closable="false"
            :title="turn.errorCode ? `提问被拒绝（${turn.errorCode}）` : '提问失败'"
            :description="turn.error"
          />

          <template v-else-if="turn.answer">
            <div v-if="turn.answer.insufficient || isRetrievalResult(turn.answer)" class="qa__answer-state" role="status" :data-answer-state="turn.answer.insufficient ? 'insufficient' : 'retrieval-only'">
              <Info :size="16" aria-hidden="true" />{{ turn.answer.insufficient ? '暂未找到足够依据' : '已找到资料，尚未生成答案' }}
            </div>
            <div class="qa__answer">{{ displayAnswer(turn.answer) }}</div>

            <!-- 辅助信息默认全部收起：主视图只保留整理后的答案正文。 -->
            <el-collapse class="qa__supporting-details">
              <el-collapse-item
                v-if="!turn.answer.insufficient && turn.answer.points.length"
                :name="`points-${turn.id || index}`"
              >
                <template #title>
                  <span class="qa__detail-title">答案要点（{{ turn.answer.points.length }} 条）</span>
                </template>
                <ul class="qa__points">
                  <li v-for="point in turn.answer.points" :key="point">{{ point }}</li>
                </ul>
              </el-collapse-item>

              <el-collapse-item :name="`citations-${turn.id || index}`">
                <template #title>
                  <span class="qa__detail-title">
                    {{ turn.answer.insufficient || isRetrievalResult(turn.answer) ? '相关材料' : '引用来源' }}（{{ turn.answer.citations.length }} 条）
                  </span>
                </template>
                <div v-if="turn.answer.citations.length" class="qa__citations">
                  <p v-if="turn.answer.insufficient" class="pic-muted">这些材料仅供查阅，尚不足以支撑当前问题的确定结论。</p>
                  <div v-for="item in turn.answer.citations" :key="item.chunk_id" class="qa__citation">
                    <div class="qa__citation-head">
                      <strong>[{{ item.index }}] {{ citationText(item) }}</strong>
                    </div>
                    <div class="qa__citation-snippet">{{ item.snippet }}</div>
                  </div>
                </div>
                <div v-else class="pic-muted">没有可展示的引用来源</div>
              </el-collapse-item>
              <el-collapse-item v-if="turn.answer.insufficient" :name="`retrieval-${turn.id || index}`" class="qa__retrieval-details">
                <template #title><span class="qa__detail-title">检索说明（排查用）</span></template>
                <p class="pic-muted">以下是本次检索的原始判定信息；关键词覆盖比例不代表回答准确率或召回率。</p>
                <p class="pic-pre">{{ turn.answer.answer }}</p>
                <ul class="qa__points"><li v-for="item in turn.answer.missing" :key="item">{{ item }}</li></ul>
              </el-collapse-item>
            </el-collapse>

            <div style="margin-top: 10px">
              <Button v-if="canCreateTask" size="sm" variant="outline" @click="toDiagnosis(turn.question)">转成完整诊断<ChevronRight :size="14" /></Button>
            </div>
          </template>
        </el-card>
      </div>
    </div>

    <!-- 底部：提问框（贴底 + 居中） -->
    <div class="qa__composer">
      <div class="qa__composer-inner">
        <el-input
          ref="questionRef"
          v-model="question"
          aria-label="输入知识库问题"
          type="textarea"
          :rows="2"
          :disabled="conversationBusy"
          resize="none"
          placeholder="输入你的问题，也可以基于当前会话继续追问…"
          @keydown.ctrl.enter="send()"
        />
        <div class="qa__composer-bar">
          <span class="pic-muted">
            检索范围：{{ scopeSummary }} · 引用 {{ topK }} 条 ·
            {{ retrievalOnlyMode || !useModel ? '资料检索' : '请求模型归纳' }}
          </span>
          <Button :disabled="conversationBusy || sending" @click="send()"><LoaderCircle v-if="conversationBusy || sending" :size="15" class="animate-spin" /><Send v-else :size="14" />{{ sending ? '回答中…' : '提问' }}<kbd>Ctrl ↵</kbd></Button>
        </div>
      </div>
    </div>
    </section>
  </div>
</template>

<style scoped>
.qa { display: flex; flex-direction: row; height: 100%; min-height: 0; background: #fff; }
.qa__conversations { flex: 0 0 244px; min-width: 0; display: flex; flex-direction: column; background: #fcfcfd; border-right: 1px solid #e4e4e7; }
.qa__conversations-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 22px 14px 15px; }
.qa__conversations-head strong { display: flex; align-items: center; gap: 7px; font-size: 12px; font-weight: 600; }
.qa__conversations-head :deep(button) { font-size: 12px; padding: 0 8px; height: 28px; gap: 4px; }
.qa__history-search { position: relative; margin: 0 14px 14px; }
.qa__history-search > svg { position: absolute; top: 10px; left: 10px; color: #71717a; z-index: 1; }
.qa__history-search :deep(input) { padding-left: 31px; height: 33px; font-size: 12px; background: #fff; }
.qa__conversation-list { flex: 1 1 auto; overflow-y: auto; padding: 0 8px; }
.qa__conversation-item { width: 100%; display: flex; flex-direction: column; gap: 7px; padding: 12px 10px; margin: 0 0 5px; text-align: left; color: #3f3f46; background: transparent; border: 1px solid transparent; border-radius: 6px; cursor: pointer; }
.qa__conversation-item:hover { background: #f4f4f5; }
.qa__conversation-item.is-active { background: #ededf0; border-color: #e4e4e7; color: #18181b; }
.qa__conversation-item:disabled { cursor: not-allowed; opacity: .65; }
.qa__conversation-title { width: 100%; display: block; overflow: hidden; font-size: 12px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.qa__conversation-title > svg { vertical-align: -2px; margin-right: 6px; color: #71717a; }
.qa__conversation-meta { color: #62626b; font-size: 12px; padding-left: 20px; }
.qa__conversation-empty, .qa__conversation-loading { padding: 20px 16px; color: #71717a; font-size: 12px; line-height: 1.7; }
.qa__main { flex: 1 1 auto; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
.qa__head { flex: 0 0 auto; background: #fff; border-bottom: 1px solid #ededf0; padding: 20px 28px; }
.qa__head-inner { max-width: 900px; margin: 0 auto; }
.qa__title { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.qa__title h1 { margin: 0 0 5px; font-size: 16px; font-weight: 600; letter-spacing: -.4px; }
.qa__title .pic-muted { font-size: 12px; }
.qa__title :deep(button) { flex-shrink: 0; font-size: 12px; }
.qa__settings { margin-top: 18px; padding-top: 14px; border-top: 1px solid #ededf0; }
.qa__settings :deep(.el-form-item) { margin-bottom: 8px; }
.qa__stream { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 26px 28px; }
.qa__stream-inner { max-width: 820px; margin: 0 auto; }
.qa__welcome { max-width: 620px; margin: 0 auto; padding: clamp(8px, 4vh, 45px) 0 24px; text-align: center; }
.qa__welcome-mark { display: flex; align-items: center; justify-content: center; width: 58px; height: 58px; margin: 0 auto 20px; border: 1px solid #e4e4e7; border-radius: 16px; background: #fafafa; color: #3f3f46; box-shadow: 0 2px 4px #18181b03; }
.qa__welcome-badge { font-size: 12px; font-weight: 400; color: #62626b; }
.qa__welcome h2 { margin: 16px 0 10px; font-size: 28px; font-weight: 550; letter-spacing: -1px; }
.qa__welcome > p { color: #71717a; font-size: 12px; line-height: 1.8; margin: 0; }
.qa__suggestions-label { margin: 34px 0 11px; color: #71717a; font-size: 12px; text-align: left; }
.qa__examples { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }
.qa__examples button { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 62px; margin: 0; padding: 13px 15px; border: 1px solid #e4e4e7; border-radius: 8px; background: #fff; color: #52525b; font-size: 12px; line-height: 1.7; text-align: left; cursor: pointer; transition: background .15s, border-color .15s; }
.qa__examples button:last-child { grid-column: 1 / -1; min-height: 46px; }
.qa__examples button:hover { background: #fafafa; border-color: #a1a1aa; }
.qa__examples button > svg { color: #a1a1aa; }
.qa__examples button:disabled { opacity: .6; cursor: not-allowed; }
.qa__welcome-note { margin-top: 20px; color: #71717a; font-size: 12px; line-height: 1.7; }
.qa__mobile-break { display: none; }
.qa__turn + .qa__turn { margin-top: 24px; }
.qa__turn { border-color: #e4e4e7; box-shadow: none; }
.qa__turn :deep(.el-card__header) { padding: 16px 20px; background: #fafafa; }
.qa__turn :deep(.el-card__body) { padding: 22px; }
.qa__history-error { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 8px 0; color: var(--el-color-danger); }
.qa__ask { display: flex; align-items: flex-start; gap: 10px; }
.qa__ask-text { font-weight: 550; font-size: 13px; line-height: 1.7; word-break: break-word; }
.qa__timestamp { margin-left: auto; color: #71717a; font-size: 12px; font-weight: 400; white-space: nowrap; }
.qa__answer { font-size: 14px; line-height: 1.95; white-space: pre-wrap; color: #27272a; }
.qa__answer-state { display: flex; align-items: center; gap: 8px; margin-bottom: 13px; color: #79500b; font-size: 13px; font-weight: 550; }
.qa__answer-state[data-answer-state='retrieval-only'] { color: #52525b; }
.qa__retrieval-details .pic-pre { margin: 10px 0; }
.qa__title h1 { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.qa__title h1 :deep([data-slot='badge']) { font-size: 11px; font-weight: 400; color: #62626b; }
.qa__examples strong { display: block; font-size: 13px; font-weight: 550; color: #27272a; }
.qa__examples small { display: block; margin-top: 4px; font-size: 11px; color: #62626b; }
.qa__examples button[aria-pressed='true'] { border-color: #71717a; background: #fafafa; }
.qa__points { line-height: 1.8; padding-left: 18px; margin: 0; }
.qa__supporting-details { margin-top: 18px; }
.qa__detail-title { font-weight: 500; font-size: 12px; color: #62626b; }
.qa__citation + .qa__citation { margin-top: 14px; padding-top: 14px; border-top: 1px solid #ededf0; }
.qa__citation-head { line-height: 1.7; font-size: 12px; }
.qa__citation-snippet { margin-top: 7px; color: #52525b; line-height: 1.8; font-size: 12px; white-space: pre-wrap; }
.qa__composer { flex: 0 0 auto; background: #fff; padding: 14px 28px 18px; }
.qa__composer-inner { max-width: 820px; margin: 0 auto; padding: 10px 12px 9px; border: 1px solid #d4d4d8; border-radius: 12px; background: #fff; box-shadow: 0 2px 6px #18181b06; transition: border-color .15s; }
.qa__composer-inner:focus-within { border-color: #a1a1aa; box-shadow: 0 0 0 2px #e4e4e7; }
.qa__composer :deep(.el-textarea__inner) { background: transparent; box-shadow: none; padding: 7px 6px; color: #27272a; font-size: 13px; }
.qa__composer :deep(.el-textarea__inner:focus) { box-shadow: none; }
.qa__composer-bar { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 4px; padding-left: 6px; }
.qa__composer-bar .pic-muted { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.qa__composer-bar > button { flex-shrink: 0; height: 32px; font-size: 12px; }
.qa__composer-bar kbd { padding: 1px 4px; border: 1px solid #ffffff40; border-radius: 3px; font-family: inherit; font-size: 12px; color: #d4d4d8; }
@media (max-width: 1100px) { .qa__conversations { flex-basis: 216px; } .qa__head { padding: 18px; } .qa__stream { padding: 18px; } .qa__composer { padding: 12px 18px 16px; } }
@media (max-width: 900px) {
  .qa { flex-direction: column; }
  .qa__conversations { flex: 0 0 auto; width: 100%; max-height: 155px; display: grid; grid-template-columns: 1fr 1fr; border-right: 0; border-bottom: 1px solid #e4e4e7; }
  .qa__conversations-head { padding: 12px; }
  .qa__history-search { margin: 11px 12px 11px 0; align-self: center; }
  .qa__conversation-list { grid-column: 1 / -1; display: flex; gap: 7px; overflow-x: auto; overflow-y: hidden; padding: 0 10px 8px; }
  .qa__conversation-item { flex: 0 0 200px; margin: 0; padding: 8px; }
  .qa__conversation-empty, .qa__conversation-loading { grid-column: 1 / -1; padding: 0 14px 12px; }
  .qa__welcome { padding-top: 14px; }
}
@media (max-width: 520px) {
  .qa__title .pic-muted { max-width: 185px; display: block; font-size: 12px; }
  .qa__head { padding: 14px 16px; }
  .qa__settings :deep(.el-form-item) { width: 100%; margin-right: 0; }
  .qa__settings :deep(.el-form-item__content) { flex: 1; min-width: 0; }
  .qa__settings :deep(.el-select) { max-width: 100%; }
  .qa__stream { padding: 16px; }
  .qa__welcome h2 { font-size: 24px; }
  .qa__welcome > p { font-size: 12px; }
  .qa__welcome-mark { width: 48px; height: 48px; border-radius: 13px; margin-bottom: 14px; }
  .qa__suggestions-label { margin-top: 24px; }
  .qa__examples { grid-template-columns: 1fr; }
  .qa__examples button { min-height: 47px; padding: 11px 12px; font-size: 12px; }
  .qa__composer { padding: 10px 12px 12px; }
  .qa__composer-bar kbd { display: none; }
  .qa__composer-bar .pic-muted { max-width: 170px; }
  .qa__timestamp { display: none; }
  .qa__turn :deep(.el-card__body) { padding: 16px; }
}
@media (max-height: 600px) {
  .qa__head { max-height: 110px; min-height: 0; overflow-y: auto; }
  .qa__stream { padding-top: 8px; padding-bottom: 8px; }
  .qa__composer { padding-top: 8px; padding-bottom: 8px; }
}
</style>

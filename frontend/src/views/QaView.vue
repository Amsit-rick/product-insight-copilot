<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { knowledgeApi, qaApi } from '@/api/endpoints'
import type {
  QaAnswer,
  QaCitation,
  QaConversation,
  QaConversationMessage,
} from '@/api/types'
import { useSessionStore } from '@/stores/session'

const router = useRouter()
const session = useSessionStore()

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
const turns = ref<Turn[]>([])
const conversations = ref<QaConversation[]>([])
const activeConversationId = ref<string | null>(null)
const knowledgeBases = ref<string[]>([])
const selectedKbs = ref<string[]>([])
const useModel = ref(true)
const topK = ref(6)
const streamRef = ref<HTMLElement | null>(null)
const conversationListLoading = ref(false)
const conversationLoading = ref(false)
const conversationError = ref('')

const EXAMPLES = [
  '埋点字典里 user_login 事件的口径是怎么定义的？',
  '3.2.0 的发布记录里写的是升级到哪个登录 SDK 版本？',
  '客服工单里用户抱怨最多的是哪几类问题？',
  '复盘写没有崩溃上升，但工单里闪退反馈翻倍，这两边矛盾吗？',
  '同赛道竞品 8 月的次日留存做到多少了？',
]

const canCreateTask = computed(() => session.can('task:create'))
const sending = computed(() => turns.value.some((item) => item.loading))
const conversationBusy = computed(
  () => conversationLoading.value || conversationListLoading.value,
)
const scopeSummary = computed(() =>
  selectedKbs.value.length ? selectedKbs.value.join('、') : '全部可见知识库',
)

onMounted(async () => {
  // 进入知识库问答时始终停留在问答首页。历史会话只展示在左侧，
  // 用户明确点击某条记录后才加载内容，避免一进页面就误入最近会话。
  void loadConversations()
  try {
    const list = await knowledgeApi.list()
    knowledgeBases.value = list.map((item: { code: string }) => item.code)
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
        <strong>问答会话</strong>
        <el-button type="primary" size="small" :disabled="sending || conversationBusy" @click="newConversation">
          新建会话
        </el-button>
      </div>
      <div v-if="conversationListLoading && !conversations.length" class="qa__conversation-loading">
        <el-skeleton :rows="4" animated />
      </div>
      <div v-else-if="!conversations.length" class="qa__conversation-empty">
        还没有归档会话
      </div>
      <div v-else class="qa__conversation-list">
        <button
          v-for="item in conversations"
          :key="item.id"
          type="button"
          class="qa__conversation-item"
          :class="{ 'is-active': item.id === activeConversationId }"
          :disabled="conversationLoading || sending"
          @click="openConversation(item.id)"
        >
          <span class="qa__conversation-title">{{ item.title }}</span>
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
          <span style="font-weight: 600">知识库问答</span>
          <span class="pic-muted">
            检索即答：答案来自引用片段，秒级返回；<b>不做冲突裁决、不进人工审核</b>，需要严谨结论请点「转成完整诊断」
          </span>
        </div>
        <el-form :inline="true" size="small" class="qa__settings">
          <el-form-item label="检索范围">
            <el-select
              v-model="selectedKbs"
              multiple
              collapse-tags
              clearable
              placeholder="默认全部可见知识库"
              style="width: 260px"
            >
              <el-option v-for="code in knowledgeBases" :key="code" :label="code" :value="code" />
            </el-select>
          </el-form-item>
          <el-form-item label="引用条数">
            <el-input-number v-model="topK" :min="1" :max="20" size="small" style="width: 110px" />
          </el-form-item>
          <el-form-item label="模型归纳">
            <el-switch v-model="useModel" />
            <span class="pic-muted" style="margin-left: 8px">
              {{ useModel ? '有模型时归纳成一段话（引用仍来自片段）' : '纯检索式：只给要点与引用' }}
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
          <el-empty description="还没有提问：从下面输入一个问题，或点一个示例问题试试">
            <div class="qa__examples">
              <el-button
                v-for="item in EXAMPLES"
                :key="item"
                size="small"
                plain
                @click="send(item)"
              >
                {{ item }}
              </el-button>
            </div>
          </el-empty>
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
            <el-alert
              v-if="turn.answer.insufficient"
              type="warning"
              :closable="false"
              title="当前知识库没有找到足以直接回答的问题依据"
              style="margin-bottom: 10px"
            >
              <div v-for="item in turn.answer.missing" :key="item">· {{ item }}</div>
            </el-alert>

            <div class="qa__answer">{{ turn.answer.answer }}</div>

            <div v-if="turn.answer.insufficient" class="qa__insufficient-context">
              下面的内容只是检索到的相关材料，用来帮助你定位资料缺口，不能当作这个问题的确定答案。
            </div>

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
                    {{ turn.answer.insufficient ? '相关材料' : '引用来源' }}（{{ turn.answer.citations.length }} 条）
                  </span>
                </template>
                <div v-if="turn.answer.citations.length" class="qa__citations">
                  <div v-for="item in turn.answer.citations" :key="item.chunk_id" class="qa__citation">
                    <div class="qa__citation-head">
                      <strong>[{{ item.index }}] {{ citationText(item) }}</strong>
                    </div>
                    <div class="qa__citation-snippet">{{ item.snippet }}</div>
                  </div>
                </div>
                <div v-else class="pic-muted">没有可展示的引用来源</div>
              </el-collapse-item>
            </el-collapse>

            <div style="margin-top: 10px">
              <el-button v-if="canCreateTask" type="primary" plain @click="toDiagnosis(turn.question)">
                转成完整诊断
              </el-button>
              <span v-else class="pic-muted">当前账号无 task:create 权限，无法转成完整诊断</span>
            </div>
          </template>
        </el-card>
      </div>
    </div>

    <!-- 底部：提问框（贴底 + 居中） -->
    <div class="qa__composer">
      <div class="qa__composer-inner">
        <el-input
          v-model="question"
          type="textarea"
          :rows="2"
          :disabled="conversationBusy"
          resize="none"
          placeholder="问一句：例如「埋点字典里 user_login 事件的口径是怎么定义的？」（Ctrl+Enter 发送）"
          @keydown.ctrl.enter="send()"
        />
        <div class="qa__composer-bar">
          <span class="pic-muted">
            检索范围：{{ scopeSummary }} · 引用 {{ topK }} 条 ·
            {{ useModel ? '模型归纳' : '纯检索式' }}
          </span>
          <el-button
            type="primary"
            :loading="conversationBusy || sending"
            :disabled="conversationBusy"
            @click="send()"
          >
            提问（Ctrl+Enter）
          </el-button>
        </div>
      </div>
    </div>
    </section>
  </div>
</template>

<style scoped>
/* 聊天式三段布局：顶栏（设置）+ 中间（对话，自己滚）+ 底部（提问框，贴底居中）。
   整页高度锁死为可视区高度，只有中间那段滚动 —— 这样提问框永远在手边。 */
.qa {
  display: flex;
  flex-direction: row;
  height: 100%;
  min-height: 0;
  background: var(--pic-bg);
}

.qa__conversations {
  flex: 0 0 260px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: #fff;
  border-right: 1px solid var(--el-border-color-light);
}

.qa__conversations-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 14px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.qa__conversation-list {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 8px;
}

.qa__conversation-item {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 10px;
  margin: 0 0 6px;
  text-align: left;
  color: var(--el-text-color-primary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
}

.qa__conversation-item:hover {
  background: var(--el-fill-color-light);
}

.qa__conversation-item.is-active {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-5);
}

.qa__conversation-item:disabled {
  cursor: not-allowed;
  opacity: 0.65;
}

.qa__conversation-title {
  width: 100%;
  overflow: hidden;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qa__conversation-meta,
.qa__conversation-empty {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.qa__conversation-empty,
.qa__conversation-loading {
  padding: 16px 12px;
}

.qa__main {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.qa__head {
  flex: 0 0 auto;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  padding: 12px 20px 4px;
}

.qa__head-inner {
  max-width: 960px;
  margin: 0 auto;
}

.qa__title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}

.qa__settings {
  margin-top: 8px;
}

.qa__settings :deep(.el-form-item) {
  margin-bottom: 8px;
}

.qa__stream {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 20px;
}

.qa__stream-inner {
  max-width: 960px;
  margin: 0 auto;
}

.qa__welcome {
  padding-top: 24px;
}

.qa__examples {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: center;
}

.qa__examples :deep(.el-button) {
  margin-left: 0;
  max-width: 560px;
}

.qa__turn + .qa__turn {
  margin-top: 14px;
}

.qa__history-more,
.qa__history-error {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
}

.qa__history-error {
  color: var(--el-color-danger);
}

.qa__ask {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.qa__ask-text {
  font-weight: 600;
  line-height: 1.6;
  word-break: break-word;
}

.qa__timestamp {
  margin-left: auto;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: 400;
  white-space: nowrap;
}

.qa__answer {
  font-size: 15px;
  line-height: 1.7;
  white-space: pre-wrap;
}

.qa__insufficient-context {
  margin-top: 10px;
  padding: 10px 12px;
  color: #8a5a00;
  background: #fff8e6;
  border: 1px solid #f3d38a;
  border-radius: 4px;
  line-height: 1.6;
}

.qa__points {
  line-height: 1.7;
  padding-left: 18px;
  margin: 0;
}

.qa__supporting-details {
  margin-top: 14px;
}

.qa__detail-title {
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.qa__citation + .qa__citation {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.qa__citation-head {
  line-height: 1.6;
}

.qa__citation-snippet {
  margin-top: 6px;
  color: var(--el-text-color-regular);
  line-height: 1.7;
  white-space: pre-wrap;
}

.qa__composer {
  flex: 0 0 auto;
  background: #fff;
  border-top: 1px solid #e4e7ed;
  padding: 12px 20px 14px;
}

/* 居中靠这三行：限宽 + 左右 auto 外边距 */
.qa__composer-inner {
  max-width: 960px;
  margin: 0 auto;
}

.qa__composer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
  flex-wrap: wrap;
}

/* 中小屏不再同时保留“主导航 + 260px 会话栏 + 对话区”三列。
   800px 宽时三列会把对话区压到约 330px，设置项与输入工具条连续换行，
   最终把提问框挤出可视区。改成横向会话带后，历史会话仍可切换，主区也能正常使用。 */
@media (max-width: 900px) {
  .qa {
    flex-direction: column;
  }

  .qa__conversations {
    flex: 0 0 auto;
    width: 100%;
    max-height: 148px;
    border-right: 0;
    border-bottom: 1px solid var(--el-border-color-light);
  }

  .qa__conversation-list {
    display: flex;
    gap: 8px;
    overflow-x: auto;
    overflow-y: hidden;
  }

  .qa__conversation-item {
    flex: 0 0 220px;
    margin-bottom: 0;
  }
}

/* 矮窗口（分屏、远程桌面、自动化浏览器）优先保证“答案区 + 提问框”可见。
   设置项仍可在自己的区域滚动，不会被隐藏或丢失。 */
@media (max-height: 600px) {
  .qa__head {
    max-height: 110px;
    min-height: 0;
    overflow-y: auto;
  }

  .qa__stream {
    padding-top: 8px;
    padding-bottom: 8px;
  }

  .qa__composer {
    padding-top: 8px;
    padding-bottom: 8px;
  }
}
</style>


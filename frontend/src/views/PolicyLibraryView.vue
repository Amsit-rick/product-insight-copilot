<script setup lang="ts">
/**
 * 售后政策库管理页（需求书 §6 政策库 / §7 知识库页"检索测试"）。
 *
 * 这一页存在的理由：`policy_kb` 的接口早就齐了（清单含可用性判定、检索测试含被排除版本、
 * 归档/启用、新建），但界面上只有一张"现状"卡片，于是"生效期 + 适用范围过滤"这项合规能力
 * **只能在接口层验证**。这一页把三件事搬上台面：
 *
 * 1. **上下文就是过滤条件**：`shop_id` / `platform` / `at` 三个参数直接透传给
 *    `GET /policies`，可用性结论完全由后端 `applicability()` 给出 —— 前端不自己算一遍，
 *    否则界面与检索层会出现两套判定（一处说能用、一处不给用）；
 * 2. **reason 原样显示**：它是判定依据（"已失效（2026-06-30 失效）"），
 *    在界面上重写文案等于把依据改成结论，所以这里一个字都不改；
 * 3. **检索测试**：客户问题 → 命中片段 + **被排除的政策版本与原因**，
 *    这是"失效政策进不了答案"的界面证据。
 */

import { computed, onMounted, ref } from 'vue'
import { afterSalesPolicyApi } from '@/api/endpoints'
import { useSessionStore } from '@/stores/session'
import type { AfterSalesPolicyOut, PolicySearchTestOut } from '@/api/types'

const session = useSessionStore()

// 权限判断方式照抄 App.vue 的 canEval / canAdmin：只读 session.actions，
// 后端仍是唯一裁决方，这里只用来隐藏/禁用入口。
const canWrite = computed(() => session.can('policy:write'))
const canRead = computed(() => session.can('policy:read'))

// ---------------------------------------------------------------- 上下文（适用范围过滤）
/**
 * 四个上下文参数 = 适用范围过滤的界面入口。
 * `at` 用 `YYYY-MM-DDTHH:mm:ss`（后端是 datetime）。清空时 el-date-picker 会置 null，
 * 所以取用时统一 String(...) 兜一层；`http` 客户端会丢掉空值，等于"不传该参数"。
 */
const context = ref<{ shop_id: string; platform: string; member_level: string; at: string | null }>({
  shop_id: '',
  platform: '',
  member_level: '',
  at: '',
})

const policies = ref<AfterSalesPolicyOut[]>([])
const loading = ref(false)
const loadError = ref('')
const total = ref(0)
const usableCount = ref(0)
/** 后端返回的判定时点（`at` 字段）—— 用后端的口径，前端不自己取本地时间 */
const evaluatedAt = ref('')

const SHOP_PRESETS = ['self_operated', 'pop_shop_1']
const PLATFORM_PRESETS = ['Android', 'iOS', '全平台']

function queryContext(): { shop_id?: string; platform?: string; member_level?: string; at?: string } {
  return {
    shop_id: context.value.shop_id.trim() || undefined,
    platform: context.value.platform.trim() || undefined,
    member_level: context.value.member_level.trim() || undefined,
    at: context.value.at ? String(context.value.at) : undefined,
  }
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const response = await afterSalesPolicyApi.list(queryContext())
    policies.value = response.items
    total.value = response.count
    usableCount.value = response.usable_count
    evaluatedAt.value = response.at
  } catch (error) {
    policies.value = []
    total.value = 0
    usableCount.value = 0
    evaluatedAt.value = ''
    loadError.value = (error as Error).message
    showToast(`政策清单加载失败：${loadError.value}`)
  } finally {
    loading.value = false
  }
}

/** 「按此上下文重算」：列表与检索测试都用同一份上下文，两处结论不会互相打架 */
async function applyContext(): Promise<void> {
  await load()
  if (searchResult.value) await runSearchTest()
  showToast('已按当前上下文重算可用性')
}

function clearContext(): void {
  context.value = { shop_id: '', platform: '', member_level: '', at: '' }
  void applyContext()
}

// ---------------------------------------------------------------- 展示映射（只做映射，不改判定）
const STATUS_TEXT: Record<string, string> = {
  draft: '草稿（未生效）',
  active: '启用中',
  expired: '已过期',
  archived: '已归档',
}

const SCOPE_LABEL: Record<string, string> = {
  platform: '平台',
  shop_id: '店铺',
  member_level: '会员等级',
}

/** 可用性标签：与既有"现状"卡片同一套映射（可用 / 已失效 / 未生效 / 不可用） */
function usabilityLabel(item: AfterSalesPolicyOut): string {
  if (item.usable) return '可用'
  if (item.state === 'expired') return '已失效'
  if (item.state === 'future') return '未生效'
  return '不可用'
}

function usabilityTagType(item: AfterSalesPolicyOut): 'success' | 'warning' | 'danger' | 'info' {
  if (item.usable) return 'success'
  if (item.state === 'expired') return 'info'
  if (item.state === 'future') return 'warning'
  return 'danger'
}

function scopeEntries(scope: Record<string, unknown> | null | undefined): Array<{ key: string; label: string; text: string }> {
  const entries = Object.entries(scope ?? {})
  return entries.map(([key, value]) => ({
    key,
    label: SCOPE_LABEL[key] ?? key,
    text: scopeValueText(value),
  }))
}

/** 范围值原样呈现：数组用 / 连接；null / 空串是后端语义里的"不限"，所以标成"不限"而不是"—" */
function scopeValueText(value: unknown): string {
  if (Array.isArray(value)) return value.length ? value.map((item) => String(item)).join(' / ') : '不限'
  if (value === null || value === undefined) return '不限'
  const text = String(value).trim()
  return text === '' ? '不限' : text
}

function dayText(value?: string | null): string {
  if (!value) return ''
  return value.slice(0, 10)
}

function effectiveText(item: AfterSalesPolicyOut): string {
  const from = dayText(item.effective_from) || '—'
  const to = dayText(item.effective_to) || '长期有效'
  return `${from} ~ ${to}`
}

// ---------------------------------------------------------------- toast（照抄 TicketWorkbenchView 的实现）
const toast = ref<{ text: string; visible: boolean }>({ text: '', visible: false })
let toastTimer: number | undefined

function showToast(text: string): void {
  toast.value = { text, visible: true }
  if (toastTimer) window.clearTimeout(toastTimer)
  // 原型是底部居中约 2.4 秒消失
  toastTimer = window.setTimeout(() => (toast.value.visible = false), 2400)
}

// ---------------------------------------------------------------- 归档 / 启用
const actionVisible = ref(false)
const actionTarget = ref<AfterSalesPolicyOut | null>(null)
const actionNext = ref<'active' | 'archived'>('archived')
const actionReason = ref('')
const actionSubmitting = ref(false)

function askToggleStatus(item: AfterSalesPolicyOut): void {
  if (!canWrite.value) return
  actionTarget.value = item
  // 已归档 → 启用；其余（含已过期、未生效的草稿）→ 归档
  actionNext.value = item.status === 'archived' ? 'active' : 'archived'
  actionReason.value = ''
  actionVisible.value = true
}

const actionTitle = computed(() =>
  actionNext.value === 'archived' ? '归档政策版本' : '启用政策版本',
)

async function confirmToggleStatus(): Promise<void> {
  const item = actionTarget.value
  if (!item) return
  const reason = actionReason.value.trim()
  if (!reason) {
    showToast('请填写变更理由')
    return
  }
  actionSubmitting.value = true
  try {
    await afterSalesPolicyApi.setStatus(item.id, actionNext.value, reason)
    actionVisible.value = false
    showToast(
      actionNext.value === 'archived'
        ? `已归档 ${item.title} ${item.version}`
        : `已启用 ${item.title} ${item.version}`,
    )
    await load()
  } catch (error) {
    showToast(`操作失败：${(error as Error).message}`)
  } finally {
    actionSubmitting.value = false
  }
}

// ---------------------------------------------------------------- 检索测试
const searchQuery = ref('物流停滞超过 48 小时可以补偿多少钱')
const searchLoading = ref(false)
const searchResult = ref<PolicySearchTestOut | null>(null)

/** document_id → "标题 版本"：命中片段只给 source_name，标题要从清单里对回来 */
const policyLabelByDocument = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  for (const item of policies.value) {
    if (item.document_id) map[item.document_id] = `${item.title} ${item.version}`
  }
  return map
})

function hitTitle(documentId: string, sourceName: string): string {
  return policyLabelByDocument.value[documentId] ?? sourceName
}

async function runSearchTest(): Promise<void> {
  const query = searchQuery.value.trim()
  if (query.length < 2) {
    showToast('请输入至少 2 个字的客户问题')
    return
  }
  searchLoading.value = true
  try {
    // 与清单用同一份上下文：这样"清单说不可用"与"检索被排除"必然一致
    searchResult.value = await afterSalesPolicyApi.searchTest({ query, top_k: 5, ...queryContext() })
  } catch (error) {
    searchResult.value = null
    showToast(`检索测试失败：${(error as Error).message}`)
  } finally {
    searchLoading.value = false
  }
}

// ---------------------------------------------------------------- 新建政策
/**
 * 后端 `POST /policies/documents` 的入参是纯 JSON（title/version/content/effective_from/
 * effective_to/scope/note），没有文件上传，所以新建政策可以在界面做完。
  * `scope` 是自由 dict，这里只暴露后端 `applicability()` 真正会判定的三个范围键。
 */
const createVisible = ref(false)
const creating = ref(false)
const createForm = ref({
  title: '',
  version: '',
  content: '',
  effective_from: '',
  effective_to: '',
  platform: '',
  shop_id: '',
  member_level: '',
})

function applyCreateTemplate(): void {
  createForm.value = {
    title: '物流时效与补偿标准',
    version: 'v5.0',
    content:
      '1. 物流停滞认定\n1.1 包裹在运输途中连续 48 小时无轨迹更新的，认定为「物流停滞」。\n\n' +
      '2. 补偿标准\n2.1 停滞超过 48 小时：按订单实付金额的 5% 补偿，单笔不超过 ¥30。',
    effective_from: '2027-01-01',
    effective_to: '',
    platform: '全平台',
    shop_id: 'self_operated',
    member_level: '',
  }
}

async function submitCreate(): Promise<void> {
  const form = createForm.value
  const title = form.title.trim()
  const version = form.version.trim()
  const content = form.content.trim()
  if (title.length < 2) {
    showToast('标题至少 2 个字')
    return
  }
  if (!version) {
    showToast('请填写版本号（如 v5.0）')
    return
  }
  if (content.length < 10) {
    showToast('政策正文至少 10 个字（后端校验）')
    return
  }
  if (form.effective_from && form.effective_to && form.effective_to < form.effective_from) {
    showToast('失效日期不能早于生效日期')
    return
  }
  // 只把填了的范围键传上去：空串会被后端当成"限定了范围但值不匹配"，与"不限"不是一回事
  const scope: Record<string, string> = {}
  if (form.platform.trim()) scope.platform = form.platform.trim()
  if (form.shop_id.trim()) scope.shop_id = form.shop_id.trim()
  if (form.member_level.trim()) scope.member_level = form.member_level.trim()

  creating.value = true
  try {
    await afterSalesPolicyApi.create({
      title,
      version,
      content,
      effective_from: form.effective_from || undefined,
      effective_to: form.effective_to || undefined,
      scope,
    })
    createVisible.value = false
    showToast(`已新建政策 ${title} ${version}`)
    createForm.value = {
      title: '',
      version: '',
      content: '',
      effective_from: '',
      effective_to: '',
      platform: '',
      shop_id: '',
      member_level: '',
    }
    await load()
  } catch (error) {
    showToast(`新建失败：${(error as Error).message}`)
  } finally {
    creating.value = false
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="pl" v-loading="loading">
    <div class="pic-page-title">
      <h2>售后政策库</h2>
      <span class="pic-muted">policy_kb · 版本 / 生效期 / 适用范围 / 检索测试</span>
    </div>

    <!-- 这一页的立场：政策依据必须来自现行且在适用范围内的版本 -->
    <el-alert
      type="warning"
      :closable="false"
      title="政策依据必须来自现行且在适用范围内的版本，引用已失效 / 未生效版本属合规事故。"
      description="下面的「可用 / 已失效 / 未生效 / 不可用」与原因文字全部由后端 applicability() 判定后返回（含归档、生效期、店铺与平台范围），本页只做展示，不重复实现判定。"
    />

    <el-alert
      v-if="!canRead"
      class="pic-section-gap"
      type="error"
      :closable="false"
      title="当前账号没有 policy:read，接口会返回 403"
      description="政策库需要 policy:read（客服 / 产品 / 主管 / 管理员有），修订政策需要 policy:write（主管 / 管理员有）。"
    />

    <!-- 上下文条：适用范围过滤的界面入口 -->
    <el-card shadow="never" class="pic-section-gap">
      <template #header>
        <span style="font-weight: 600">适用上下文（适用范围过滤）</span>
        <span class="pic-muted" style="margin-left: 10px">
           这四个参数就是"按谁的时间、哪家店、哪个平台、哪个会员等级"去判定政策能不能用；改动后点「按此上下文重算」
        </span>
      </template>

      <div class="ctx">
        <div class="ctx__item">
          <span class="ctx__label">shop_id（店铺）</span>
          <el-select
            v-model="context.shop_id"
            filterable
            allow-create
            clearable
            default-first-option
            placeholder="如 self_operated / pop_shop_1"
            style="width: 100%"
          >
            <el-option v-for="item in SHOP_PRESETS" :key="item" :label="item" :value="item" />
          </el-select>
        </div>
        <div class="ctx__item">
          <span class="ctx__label">platform（平台）</span>
          <el-select
            v-model="context.platform"
            filterable
            allow-create
            clearable
            default-first-option
            placeholder="如 Android / iOS"
            style="width: 100%"
          >
            <el-option v-for="item in PLATFORM_PRESETS" :key="item" :label="item" :value="item" />
          </el-select>
        </div>
        <div class="ctx__item">
          <span class="ctx__label">member_level（会员等级）</span>
          <el-select
            v-model="context.member_level"
            filterable
            allow-create
            clearable
            default-first-option
            placeholder="如 gold / silver"
            style="width: 100%"
          >
            <el-option label="gold" value="gold" />
            <el-option label="silver" value="silver" />
          </el-select>
        </div>
        <div class="ctx__item">
          <span class="ctx__label">at（判定时点）</span>
          <el-date-picker
            v-model="context.at"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ss"
            placeholder="留空 = 现在"
            clearable
            style="width: 100%"
          />
        </div>
        <div class="ctx__actions">
          <el-button type="primary" @click="applyContext">按此上下文重算</el-button>
          <el-button @click="clearContext">清空上下文</el-button>
        </div>
      </div>

      <div class="pic-muted" style="margin-top: 10px">
         当前上下文：店铺 {{ context.shop_id || '未提供' }} · 平台 {{ context.platform || '未提供' }} · 会员等级 {{ context.member_level || '未提供' }} ·
        判定时点 {{ context.at || '现在' }}；后端本次判定时点 at = {{ evaluatedAt || '—' }}，
        共 {{ total }} 个版本，其中可用 {{ usableCount }} 个。
      </div>
    </el-card>

    <!-- 政策列表 -->
    <el-card shadow="never" class="pic-section-gap">
      <template #header>
        <span style="font-weight: 600">政策版本（{{ total }}）</span>
        <span class="pic-muted" style="margin-left: 10px">
          「可用性」与「判定原因」是后端返回的原文，未做二次改写
        </span>
      </template>

      <el-table :data="policies" size="small" empty-text="没有政策版本">
        <el-table-column label="标题" min-width="180">
          <template #default="{ row }">
            <span class="cell-strong">{{ row.title }}</span>
          </template>
        </el-table-column>
        <el-table-column label="版本" width="90">
          <template #default="{ row }">
            <span class="pic-mono">{{ row.version }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag size="small" type="info" :title="row.status">
              {{ STATUS_TEXT[row.status] ?? row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="生效期" width="200">
          <template #default="{ row }">
            <span class="pic-mono">{{ effectiveText(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="适用范围" min-width="200">
          <template #default="{ row }">
            <div v-if="scopeEntries(row.scope).length" class="scope">
              <span v-for="entry in scopeEntries(row.scope)" :key="entry.key" class="scope__item">
                {{ entry.label }}：{{ entry.text }}
              </span>
            </div>
            <span v-else class="pic-muted">不限（未限定范围）</span>
          </template>
        </el-table-column>
        <el-table-column label="可用性" width="230">
          <template #default="{ row }">
            <el-tag size="small" :type="usabilityTagType(row)">{{ usabilityLabel(row) }}</el-tag>
            <!-- reason 是判定依据，原样显示 -->
            <div class="reason">{{ row.reason }}</div>
            <div v-if="row.note" class="pic-muted">{{ row.note }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="110">
          <template #default="{ row }">
            <el-button
              size="small"
              :type="row.status === 'archived' ? 'success' : 'warning'"
              :disabled="!canWrite"
              :title="canWrite ? '' : '当前账号没有 policy:write，不能改政策状态'"
              @click="askToggleStatus(row)"
            >
              {{ row.status === 'archived' ? '启用' : '归档' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pic-muted" style="margin-top: 8px">
        <template v-if="canWrite">你可以归档 / 启用政策版本（需要 policy:write）。</template>
        <template v-else>当前账号没有 policy:write（只读），归档 / 启用按钮不可用。</template>
      </div>
    </el-card>

    <!-- 检索测试 -->
    <el-card shadow="never" class="pic-section-gap">
      <template #header>
        <span style="font-weight: 600">检索测试</span>
        <span class="pic-muted" style="margin-left: 10px">
          输入一句客户问题，看命中哪些政策片段；<b>被排除的版本与原因</b>就是"失效政策进不了答案"的证据
        </span>
      </template>

      <div class="search-bar">
        <el-input
          v-model="searchQuery"
          placeholder="例如：物流停滞超过 48 小时可以补偿多少钱"
          clearable
          @keyup.enter="runSearchTest"
        />
        <el-button type="primary" :loading="searchLoading" @click="runSearchTest">检索测试</el-button>
      </div>
      <div class="pic-muted" style="margin-top: 6px">
         检索使用与上方完全相同的上下文（shop_id / platform / member_level / at）；排除逻辑在后端下推到检索层，不是事后过滤。
      </div>

      <template v-if="searchResult">
        <div class="search-meta">
          查询「{{ searchResult.query }}」· 判定时点 at = {{ searchResult.at }} · 店铺
           {{ searchResult.shop_id || '未提供' }} · 平台 {{ searchResult.platform || '未提供' }} · 会员等级
           {{ searchResult.member_level || '未提供' }} ·
          命中 {{ searchResult.hits.length }} 条
        </div>

        <h4 class="sub">命中片段（{{ searchResult.hits.length }}）</h4>
        <el-table :data="searchResult.hits" size="small" empty-text="没有命中任何政策片段">
          <el-table-column label="标题 / 版本" width="230">
            <template #default="{ row }">
              <div class="cell-strong">{{ hitTitle(row.document_id, row.source_name) }}</div>
              <span class="pic-mono">{{ row.policy_version || '—' }}</span>
              <span class="pic-muted"> · 生效期 {{ dayText(row.effective_from) || '—' }} ~
                {{ dayText(row.effective_to) || '长期有效' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="命中小节" width="150">
            <template #default="{ row }">
              <span class="pic-muted">{{ row.section_title || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="片段文本" min-width="320">
            <template #default="{ row }">
              <div class="snippet">{{ row.snippet }}</div>
            </template>
          </el-table-column>
          <el-table-column label="可用性" width="200">
            <template #default="{ row }">
              <el-tag size="small" :type="row.usable ? 'success' : 'danger'">
                {{ row.usable ? '可用' : '不可用' }}
              </el-tag>
              <div class="reason">{{ row.reason }}</div>
            </template>
          </el-table-column>
        </el-table>

        <h4 class="sub">
          被排除的政策版本（{{ searchResult.excluded_policies.length }}）
          <span class="pic-muted">共排除 {{ searchResult.excluded_document_ids.length }} 个政策文档</span>
        </h4>
        <el-table
          :data="searchResult.excluded_policies"
          size="small"
          empty-text="本次没有被排除的政策版本（当前上下文下所有版本都可用）"
        >
          <el-table-column label="标题 / 版本" width="230">
            <template #default="{ row }">
              <div class="cell-strong">{{ row.title }}</div>
              <span class="pic-mono">{{ row.version }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag size="small" type="info" :title="row.status">
                {{ STATUS_TEXT[row.status] ?? row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="生效期" width="200">
            <template #default="{ row }">
              <span class="pic-mono">{{ effectiveText(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="排除原因（后端原文）" min-width="300">
            <template #default="{ row }">
              <div class="reason reason--strong">{{ row.reason }}</div>
            </template>
          </el-table-column>
        </el-table>
      </template>
      <div v-else class="pic-muted" style="margin-top: 10px">还没有跑过检索测试。</div>
    </el-card>

    <!-- 新建政策 -->
    <el-card v-if="canWrite" shadow="never" class="pic-section-gap">
      <template #header>
        <span style="font-weight: 600">新建政策</span>
        <el-button type="primary" size="small" style="float: right" @click="createVisible = true">
          新建政策
        </el-button>
      </template>
      <div class="pic-muted">
        入参为 JSON：title / version / content（Markdown 正文，≥10 字）/ effective_from /
        effective_to / scope{platform, shop_id, member_level}；没有文件上传。后端会按生效期自动置为
        启用中（今天生效）/ 已过期（失效日在今天之前）/ 草稿·未生效（生效日在今天之后）。
        新建后该版本立即进入上方清单并参与检索判定。
      </div>
    </el-card>
    <el-alert
      v-else
      class="pic-section-gap"
      type="info"
      :closable="false"
      title="新建政策需要 policy:write，当前账号只读"
      description="后端 POST /policies/documents 需要 policy:write（主管 / 管理员）。本页对只读账号不放置任何点了没用的按钮。"
    />

    <!-- 归档 / 启用确认框：理由必填 -->
    <el-dialog v-model="actionVisible" :title="actionTitle" width="480px" :show-close="false" class="policy-action-dialog">
      <p class="pic-muted">
        {{
          actionNext === 'archived'
            ? `归档后该版本立即变为不可用，检索层会把它排除（政策依据不得引用已归档版本）。`
            : `启用后该版本可能重新进入可用范围（仍受生效期与适用范围约束）。`
        }}
      </p>
      <p v-if="actionTarget" class="target">
        {{ actionTarget.title }} <span class="pic-mono">{{ actionTarget.version }}</span> ·
        当前状态 {{ STATUS_TEXT[actionTarget.status] ?? actionTarget.status }}
      </p>
      <el-input
        v-model="actionReason"
        type="textarea"
        :rows="3"
        placeholder="填写变更理由（必填）"
      />
      <p class="pic-muted policy-action-note" style="margin-bottom: 0">
        说明：变更理由会随 <span class="pic-mono">POST /policies/documents/{id}/status</span>
        写入审计详情，便于追溯政策版本为何被启用或归档。
      </p>
      <template #footer>
        <el-button @click="actionVisible = false">取消</el-button>
        <el-button type="primary" :loading="actionSubmitting" @click="confirmToggleStatus">确认</el-button>
      </template>
    </el-dialog>

    <!-- 新建政策表单 -->
    <el-dialog v-model="createVisible" title="新建政策版本" width="620px">
      <el-form label-width="96px">
        <el-form-item label="标题">
          <el-input v-model="createForm.title" placeholder="例如：物流时效与补偿标准" />
        </el-form-item>
        <el-form-item label="版本号">
          <el-input v-model="createForm.version" placeholder="例如：v5.0" />
        </el-form-item>
        <el-form-item label="生效起止">
          <el-date-picker
            v-model="createForm.effective_from"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="生效日期（留空 = 今天）"
            style="width: 48%"
          />
          <span class="pic-muted" style="margin: 0 6px">~</span>
          <el-date-picker
            v-model="createForm.effective_to"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="失效日期（留空 = 长期有效）"
            style="width: 48%"
          />
        </el-form-item>
        <el-form-item label="适用范围">
          <div class="scope-form">
            <el-input v-model="createForm.platform" placeholder="platform，如 全平台 / Android（留空 = 不限）" />
            <el-input v-model="createForm.shop_id" placeholder="shop_id，如 self_operated（留空 = 不限）" />
            <el-input v-model="createForm.member_level" placeholder="member_level，如 gold（留空 = 不限）" />
            <div class="pic-muted">
               member_level 会同时传给 <span class="pic-mono">GET /policies</span> 与
               <span class="pic-mono">POST /policies/search-test</span>；留空时后端按保守原则判定为「当前工单为 未知」。
            </div>
          </div>
        </el-form-item>
        <el-form-item label="政策正文">
          <el-input
            v-model="createForm.content"
            type="textarea"
            :rows="8"
            placeholder="Markdown 正文，至少 10 个字；会按既有文档入库管道切片 + 向量化"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="applyCreateTemplate">填入示例</el-button>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 底部居中 Toast（原型约定，约 2.4 秒） -->
    <div v-if="toast.visible" class="toast">{{ toast.text }}</div>
  </div>
</template>

<style scoped>
.pl {
  padding: 4px 2px 24px;
  max-width: 100%;
  min-width: 0;
  overflow-x: hidden;
}
.ctx {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 12px;
}
.ctx__item {
  flex: 1 1 220px;
  min-width: 200px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ctx__label {
  font-size: 12px;
  color: #606266;
}
.ctx__actions {
  display: flex;
  gap: 8px;
  padding-bottom: 1px;
}
.cell-strong {
  font-weight: 600;
  font-size: 13px;
}
.scope {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
}
.scope__item {
  color: #606266;
}
.reason {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
}
.reason--strong {
  color: #b88230;
}
.search-bar {
  display: flex;
  gap: 8px;
}
.search-meta {
  margin-top: 12px;
  font-size: 12px;
  color: #606266;
}
.sub {
  margin: 16px 0 8px;
  font-size: 13px;
  font-weight: 600;
}
.sub .pic-muted {
  margin-left: 8px;
  font-weight: 400;
}
.snippet {
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.6;
  color: #303133;
  max-height: 140px;
  overflow: auto;
}
.target {
  margin: 4px 0 10px;
  font-size: 13px;
}
.scope-form {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
}

@media (max-width: 600px) {
  .pl :deep(.el-card__body) {
    min-width: 0;
    overflow-x: auto;
  }

  .pl :deep(.el-table) {
    min-width: 720px;
  }

  .pl :deep(.el-card__header) {
    line-height: 1.5;
  }

  .ctx__item {
    min-width: 0;
    flex-basis: 100%;
  }

  .ctx__actions {
    width: 100%;
  }

  .ctx__actions .el-button {
    flex: 1;
  }

  .search-bar {
    flex-direction: column;
  }

  .search-bar .el-button {
    width: 100%;
  }
}
.toast {
  position: fixed;
  left: 50%;
  bottom: 24px;
  transform: translateX(-50%);
  background: #1b252d;
  color: #fff;
  padding: 9px 16px;
  border-radius: 6px;
  font-size: 13px;
  z-index: 3000;
  box-shadow: 0 8px 24px rgba(28, 39, 48, 0.24);
}

.policy-action-note {
  overflow-wrap: anywhere;
}
</style>

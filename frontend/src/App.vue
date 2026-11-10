<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { useSessionStore } from '@/stores/session'
import { systemApi } from '@/api/endpoints'

const session = useSessionStore()
const route = useRoute()
const router = useRouter()

const health = ref<{ database: string; llm: { model: string; enabled: boolean }; vector_backend: string } | null>(null)

const activeMenu = computed(() => {
  if (route.path.startsWith('/tasks/new')) return '/tasks/new'
  if (route.path.startsWith('/tasks/')) return '/'
  for (const path of ['/tickets', '/ticket-policies', '/qa', '/knowledge', '/approvals', '/quality-review', '/metrics', '/system', '/evaluations', '/policies', '/audit']) {
    if (route.path.startsWith(path)) return path
  }
  return '/'
})

// 聊天式页面（如知识库问答）自己管滚动与内边距，父容器不再叠加 padding/滚动条，
// 否则"提问框贴底"会被父级的 40px 下内边距顶起来。
const flushMain = computed(() => route.meta.flush === true)

// 管理入口按权限隐藏（后端仍是唯一裁决方）
const canAdmin = computed(() => session.can('admin:manage'))
const canEval = computed(() => session.can('eval:run'))
const canTickets = computed(() => session.can('ticket:read'))
const canLegacyPlatform = computed(
  () => canAdmin.value || session.roles.some((role) => ['pm', 'eng'].includes(role)),
)
// 售后政策库：能读政策的人就能看（客服/产品/主管/管理员都有 policy:read），修订政策才需要 policy:write
const canReadPolicies = computed(() => session.can('policy:read'))

const envLabel = computed(() => {
  if (!health.value) return '后端未连接'
  const llm = health.value.llm.enabled ? health.value.llm.model : 'stub 模型'
  return `${health.value.database} · ${llm} · 向量 ${health.value.vector_backend}`
})

// 数据库、模型和向量实现属于运维信息，客服和主管只需要处理业务工单。
const canViewRuntimeDetails = computed(
  () => canAdmin.value || session.roles.includes('eng'),
)

const ROLE_LABELS: Record<string, string> = {
  operation: '客服',
  reviewer: '售后主管',
  admin: '管理员',
  pm: '产品经理',
  product_manager: '产品经理',
  eng: '研发',
  engineer: '研发',
}

function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role
}

onMounted(async () => {
  try {
    health.value = (await systemApi.health()) as typeof health.value
  } catch {
    health.value = null
  }
})

async function logout(): Promise<void> {
  await session.logout()
  ElMessage.success('已退出登录')
  void router.push({ name: 'login' })
}
</script>

<template>
  <el-config-provider :locale="zhCn">
    <div class="pic-app">
    <header class="pic-header">
      <div class="pic-header__brand">
        电商售后工单智能处理系统
        <small>After-sales Ticket Copilot</small>
      </div>
      <div class="pic-header__right">
        <el-tag v-if="canViewRuntimeDetails" size="small" effect="dark" type="info">{{ envLabel }}</el-tag>
        <template v-if="session.loggedIn">
          <span>{{ session.displayName }}</span>
          <el-tag v-for="role in session.roles" :key="role" size="small" type="success">
            {{ roleLabel(role) }}
          </el-tag>
          <el-button link style="color: #fff" @click="logout">退出</el-button>
        </template>
      </div>
    </header>

    <div class="pic-body">
      <aside v-if="session.loggedIn" class="pic-aside">
        <el-menu :default-active="activeMenu" router>
          <!-- 售后工单是当前主线，放在第一项（诊断能力仍在，排在后面） -->
          <el-menu-item v-if="canTickets" index="/tickets">
            <el-icon><Tickets /></el-icon>
            <span>售后工单</span>
          </el-menu-item>
          <!-- 政策库紧挨售后工单：工单的「政策依据」就来自这里，两侧对照着看才验证得了生效期过滤 -->
          <el-menu-item v-if="canReadPolicies" index="/ticket-policies">
            <el-icon><Collection /></el-icon>
            <span>政策库</span>
          </el-menu-item>
          <el-menu-item v-if="canLegacyPlatform" index="/">
            <el-icon><Monitor /></el-icon>
            <span>诊断工作台</span>
          </el-menu-item>
          <el-menu-item v-if="canLegacyPlatform" index="/tasks/new">
            <el-icon><Plus /></el-icon>
            <span>新建诊断</span>
          </el-menu-item>
          <el-menu-item v-if="canTickets && (canAdmin || session.can('ticket:approve'))" index="/approvals">
            <el-icon><Stamp /></el-icon>
            <span>人工审核</span>
          </el-menu-item>
          <el-menu-item v-if="canTickets && session.can('ticket:quality_review')" index="/quality-review">
            <el-icon><DocumentChecked /></el-icon>
            <span>质检抽查</span>
          </el-menu-item>
          <el-menu-item v-if="canLegacyPlatform || canTickets" index="/qa">
            <el-icon><ChatDotRound /></el-icon>
            <span>知识库问答</span>
          </el-menu-item>
          <el-menu-item v-if="canLegacyPlatform" index="/knowledge">
            <el-icon><Files /></el-icon>
            <span>知识库管理</span>
          </el-menu-item>
          <el-menu-item v-if="canLegacyPlatform" index="/metrics">
            <el-icon><TrendCharts /></el-icon>
            <span>指标查询</span>
          </el-menu-item>
          <el-menu-item v-if="canAdmin || session.roles.includes('eng')" index="/system">
            <el-icon><Odometer /></el-icon>
            <span>系统状态</span>
          </el-menu-item>
          <el-menu-item v-if="canEval" index="/evaluations">
            <el-icon><DataAnalysis /></el-icon>
            <span>评测集</span>
          </el-menu-item>
          <el-menu-item v-if="canAdmin" index="/policies">
            <el-icon><Lock /></el-icon>
            <span>访问策略</span>
          </el-menu-item>
          <el-menu-item v-if="canAdmin" index="/audit">
            <el-icon><Document /></el-icon>
            <span>审计日志</span>
          </el-menu-item>
        </el-menu>
      </aside>

      <main class="pic-main" :class="{ 'pic-main--flush': flushMain }">
        <router-view />
      </main>
    </div>
    </div>
  </el-config-provider>
</template>

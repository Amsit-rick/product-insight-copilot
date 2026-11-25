<script setup lang="ts">
import { computed, onMounted, provide, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChevronDown, ChevronRight, Layers, LogOut, Menu, PanelLeftClose, PanelLeftOpen } from '@lucide/vue'
import { ElMessage } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import AppNavigation from '@/components/AppNavigation.vue'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { useSessionStore } from '@/stores/session'
import { systemApi } from '@/api/endpoints'
import { runtimeHealthKey, type RuntimeHealth } from '@/lib/runtime'

const session = useSessionStore()
const route = useRoute()
const router = useRouter()
const collapsed = ref(false)
const mobileOpen = ref(false)
const health = ref<RuntimeHealth | null>(null)
provide(runtimeHealthKey, health)
const showShell = computed(() => session.loggedIn && route.name !== 'login')
const activeMenu = computed(() => {
  if (route.path.startsWith('/tasks/new')) return '/tasks/new'
  if (route.path.startsWith('/tasks/')) return '/'
  return route.path
})
const flushMain = computed(() => route.meta.flush === true)
const pageTitle = computed(() => (route.meta.title as string) || '工作台')
const pageGroup = computed(() => {
  if (['/tickets', '/approvals', '/quality-review'].includes(route.path)) return '业务工作区'
  if (['/qa', '/knowledge', '/ticket-policies'].includes(route.path)) return '知识与资料'
  if (['/system', '/evaluations', '/policies', '/audit'].includes(route.path)) return '管理与运维'
  return '分析与诊断'
})
const canViewRuntimeDetails = computed(() => session.can('admin:manage') || session.roles.includes('eng'))
const runtimeLabel = computed(() => {
  if (!health.value) return '后端未连接'
  const llm = health.value.llm.enabled ? health.value.llm.model : 'stub 模型'
  return `${health.value.database} · ${llm} · 向量 ${health.value.vector_backend}`
})
const roleLabels: Record<string, string> = {
  operation: '客服', reviewer: '售后主管', admin: '管理员', pm: '产品经理',
  product_manager: '产品经理', eng: '研发', engineer: '研发',
}
const rolesText = computed(() => session.roles.map((role) => roleLabels[role] ?? role).join(' · '))
const avatarText = computed(() => session.displayName.slice(0, 1).toUpperCase())
watch(() => route.fullPath, () => { mobileOpen.value = false })
onMounted(async () => {
  try { health.value = (await systemApi.health()) as typeof health.value }
  catch { health.value = null }
})
async function logout(): Promise<void> {
  await session.logout()
  ElMessage.success('已退出登录')
  void router.push({ name: 'login' })
}
function focusContent(): void {
  document.getElementById('workspace-content')?.focus()
}
</script>

<template>
  <el-config-provider :locale="zhCn">
    <div v-if="showShell" class="pic-app pic-workspace" :class="{ 'is-sidebar-collapsed': collapsed }">
      <a class="pic-skip-link" href="#workspace-content" @click.prevent="focusContent">跳到页面内容</a>
      <aside class="pic-aside" aria-label="工作空间导航">
        <router-link class="pic-brand" :to="session.can('ticket:read') ? '/tickets' : '/'" :aria-label="collapsed ? 'Product Insight Copilot 首页' : undefined">
          <span class="pic-brand__mark"><Layers :size="21" :stroke-width="1.8" aria-hidden="true" /></span>
          <span class="pic-brand__text"><strong>Product Insight</strong><small>Copilot · 电商售后工作台</small></span>
        </router-link>
        <AppNavigation :active-path="activeMenu" :collapsed="collapsed" />
        <div class="pic-sidebar-footer">
          <DropdownMenu>
            <DropdownMenuTrigger as-child>
              <Button variant="ghost" class="pic-account" data-testid="account-menu-trigger" :aria-label="collapsed ? `${session.displayName}，账号菜单` : undefined">
                <span class="pic-avatar" aria-hidden="true">{{ avatarText }}</span>
                <span class="pic-account__text"><strong>{{ session.displayName }}</strong><small>{{ rolesText }}</small></span>
                <ChevronDown class="pic-account__chevron" :size="15" aria-hidden="true" />
                <span class="sr-only">账号菜单</span>
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent side="top" align="start" class="min-w-56">
              <DropdownMenuLabel>{{ session.displayName }} · {{ rolesText }}</DropdownMenuLabel>
              <template v-if="canViewRuntimeDetails"><DropdownMenuSeparator /><DropdownMenuLabel class="text-xs font-normal text-muted-foreground">{{ runtimeLabel }}</DropdownMenuLabel></template>
              <DropdownMenuSeparator />
              <DropdownMenuItem @select="logout"><LogOut :size="15" />退出登录</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </aside>
      <div class="pic-body">
        <header class="pic-header">
          <div class="pic-header__left">
            <Button variant="ghost" size="icon-sm" class="pic-desktop-toggle" :aria-label="collapsed ? '展开侧边导航' : '收起侧边导航'" :aria-expanded="!collapsed" @click="collapsed = !collapsed">
              <PanelLeftOpen v-if="collapsed" :size="18" /><PanelLeftClose v-else :size="18" />
            </Button>
            <Button variant="ghost" size="icon-sm" class="pic-mobile-toggle" aria-label="打开主导航" :aria-expanded="mobileOpen" @click="mobileOpen = true"><Menu :size="19" /></Button>
            <span class="pic-header__divider" aria-hidden="true" />
            <nav class="pic-breadcrumb" aria-label="当前位置"><span>{{ pageGroup }}</span><ChevronRight :size="13" aria-hidden="true" /><strong aria-current="page">{{ pageTitle }}</strong></nav>
          </div>
          <div class="pic-header__right">
            <Badge variant="outline" class="pic-service-status"><span class="pic-status-dot" :class="{ 'is-offline': !health }" />{{ health ? '服务已连接' : '服务未连接' }}</Badge>
            <Button variant="ghost" size="sm" class="pic-header-logout" @click="logout"><LogOut :size="14" />退出</Button>
          </div>
        </header>
        <main id="workspace-content" tabindex="-1" class="pic-main" :class="{ 'pic-main--flush': flushMain }"><router-view /></main>
      </div>
      <Sheet v-model:open="mobileOpen">
        <SheetContent side="left" class="pic-mobile-sidebar w-72 gap-0 p-0">
          <SheetHeader class="pic-mobile-sidebar__header">
            <SheetTitle>Product Insight Copilot</SheetTitle>
            <SheetDescription>电商售后工作台 · {{ rolesText }}</SheetDescription>
          </SheetHeader>
          <AppNavigation :active-path="activeMenu" @navigate="mobileOpen = false" />
          <Button variant="ghost" class="m-3" @click="logout"><LogOut :size="16" />退出登录</Button>
        </SheetContent>
      </Sheet>
    </div>
    <div v-else class="pic-auth-root"><router-view /></div>
  </el-config-provider>
</template>

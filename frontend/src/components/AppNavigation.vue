<script setup lang="ts">
import { computed } from 'vue'
import {
  Activity, BookOpen, ChartNoAxesCombined, ClipboardCheck, FileCheck2,
  Files, FlaskConical, History, LayoutDashboard, ListChecks, LockKeyhole,
  MessageSquareText, Plus, Ticket,
} from '@lucide/vue'
import { useSessionStore } from '@/stores/session'

withDefaults(defineProps<{ activePath: string; collapsed?: boolean }>(), { collapsed: false })
defineEmits<{ navigate: [] }>()
const session = useSessionStore()
const groups = computed(() => {
  const admin = session.can('admin:manage')
  const tickets = session.can('ticket:read')
  const legacy = admin || session.roles.some((role) => ['pm', 'eng'].includes(role))
  return [
    { label: '业务工作区', items: [
      { path: '/tickets', label: '售后工单', icon: Ticket, visible: tickets },
      { path: '/approvals', label: '人工审核', icon: ClipboardCheck, visible: tickets && (admin || session.can('ticket:approve')) },
      { path: '/quality-review', label: '质检抽查', icon: ListChecks, visible: tickets && session.can('ticket:quality_review') },
    ] },
    { label: '知识与资料', items: [
      { path: '/qa', label: '知识库问答', icon: MessageSquareText, visible: legacy || tickets },
      { path: '/ticket-policies', label: '政策库', icon: BookOpen, visible: session.can('policy:read') },
      { path: '/knowledge', label: '知识库管理', icon: Files, visible: legacy },
    ] },
    { label: '分析与诊断', items: [
      { path: '/', label: '诊断工作台', icon: LayoutDashboard, visible: legacy },
      { path: '/tasks/new', label: '新建诊断', icon: Plus, visible: legacy },
      { path: '/metrics', label: '指标查询', icon: ChartNoAxesCombined, visible: legacy },
    ] },
    { label: '管理与运维', items: [
      { path: '/system', label: '系统状态', icon: Activity, visible: admin || session.roles.includes('eng') },
      { path: '/evaluations', label: '评测集', icon: FlaskConical, visible: session.can('eval:run') },
      { path: '/policies', label: '访问策略', icon: LockKeyhole, visible: admin },
      { path: '/audit', label: '审计日志', icon: History, visible: admin },
    ] },
  ].map((group) => ({ ...group, items: group.items.filter((item) => item.visible) }))
    .filter((group) => group.items.length)
})
</script>

<template>
  <nav class="pic-navigation" :class="{ 'is-collapsed': collapsed }" aria-label="主导航">
    <section v-for="group in groups" :key="group.label" class="pic-nav-group" :aria-label="group.label">
      <div class="pic-nav-group__label">{{ group.label }}</div>
      <router-link v-for="item in group.items" :key="item.path" :to="item.path" class="pic-nav-item"
        :class="{ 'is-active': activePath === item.path }" :aria-current="activePath === item.path ? 'page' : undefined"
        :title="collapsed ? item.label : undefined" :aria-label="collapsed ? item.label : undefined" @click="$emit('navigate')">
        <component :is="item.icon" :size="17" :stroke-width="1.7" aria-hidden="true" />
        <span>{{ item.label }}</span>
        <span v-if="item.path === '/qa'" class="pic-nav-item__badge" aria-hidden="true">AI</span>
      </router-link>
    </section>
    <div class="pic-nav-note"><FileCheck2 :size="15" aria-hidden="true" /><span>涉及敏感操作，需人工确认</span></div>
  </nav>
</template>

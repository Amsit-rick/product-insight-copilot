/**
 * 路由（hash 模式）。
 *
 * 采用 hash 而不是 history：构建产物由 FastAPI 直接托管静态文件，
 * 刷新任意子页面都不需要服务端做 SPA fallback，部署路径也不敏感。
 */

import { createRouter, createWebHashHistory } from 'vue-router'
import { useSessionStore } from '@/stores/session'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true, title: '登录' },
    },
    {
      path: '/',
      name: 'workbench',
      component: () => import('@/views/WorkbenchView.vue'),
      meta: { title: '工作台' },
    },
    {
      path: '/tasks/new',
      name: 'task-create',
      component: () => import('@/views/TaskCreateView.vue'),
      meta: { title: '新建诊断' },
    },
    {
      path: '/tasks/:id',
      name: 'task-progress',
      component: () => import('@/views/TaskProgressView.vue'),
      meta: { title: '任务进度' },
    },
    {
      path: '/tasks/:id/report',
      name: 'report',
      component: () => import('@/views/ReportView.vue'),
      meta: { title: '诊断报告' },
    },
    {
      path: '/tickets',
      name: 'tickets',
      component: () => import('@/views/TicketWorkbenchView.vue'),
      meta: { title: '售后工单' },
    },
    {
      // 售后政策库（policy_kb）。路径刻意与下面的 /policies（访问策略）区分开：
      // 两者都叫"策略/政策"，但一个是数据访问授权，一个是售后政策版本库，不能混在一个页面。
      path: '/ticket-policies',
      name: 'ticket-policies',
      component: () => import('@/views/PolicyLibraryView.vue'),
      meta: { title: '政策库' },
    },
    {
      path: '/qa',
      name: 'qa',
      component: () => import('@/views/QaView.vue'),
      // flush：聊天式布局，页面自己管滚动，提问框贴底居中（见 App.vue / main.css）
      meta: { title: '知识库问答', flush: true },
    },
    {
      path: '/knowledge',
      name: 'knowledge',
      component: () => import('@/views/KnowledgeView.vue'),
      meta: { title: '知识库管理' },
    },
    {
      path: '/approvals',
      name: 'approvals',
      component: () => import('@/views/ApprovalView.vue'),
      meta: { title: '人工审核' },
    },
    {
      path: '/quality-review',
      name: 'quality-review',
      component: () => import('@/views/QualityReviewView.vue'),
      meta: { title: '质检抽查' },
    },
    {
      path: '/metrics',
      name: 'metrics',
      component: () => import('@/views/MetricsView.vue'),
      meta: { title: '指标查询' },
    },
    {
      path: '/system',
      name: 'system',
      component: () => import('@/views/SystemView.vue'),
      meta: { title: '系统状态' },
    },
    {
      path: '/evaluations',
      name: 'evaluations',
      component: () => import('@/views/EvaluationsView.vue'),
      meta: { title: '评测集' },
    },
    {
      path: '/policies',
      name: 'policies',
      component: () => import('@/views/PoliciesView.vue'),
      meta: { title: '访问策略' },
    },
    {
      path: '/audit',
      name: 'audit',
      component: () => import('@/views/AuditView.vue'),
      meta: { title: '审计日志' },
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const session = useSessionStore()
  if (to.meta.public) return true
  if (!session.me) {
    try {
      await session.loadMe()
    } catch {
      return { name: 'login', query: { redirect: to.fullPath } }
    }
  }
  // 售后客服/主管不进入历史诊断平台，避免把两个产品混在同一主流程里。
  const afterSalesOnly = session.roles.some((role) => ['operation', 'reviewer'].includes(role)) && !session.can('admin:manage')
  if (afterSalesOnly) {
    const afterSalesPaths = ['/tickets', '/ticket-policies', '/qa']
    if (session.can('ticket:approve')) afterSalesPaths.push('/approvals')
    if (session.can('ticket:quality_review')) afterSalesPaths.push('/quality-review')
    const allowed = afterSalesPaths.some(
      (path) => to.path === path || to.path.startsWith(`${path}/`),
    )
    if (!allowed) return { path: '/tickets' }
  }
  return true
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? ''
  document.title = title ? `${title} · 电商售后工作台` : '电商售后工作台'
})

export default router

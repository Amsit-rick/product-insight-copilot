<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowRight, Check, ChevronRight, Eye, EyeOff, Layers, LoaderCircle, ShieldCheck } from '@lucide/vue'
import { ElMessage } from 'element-plus'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Separator } from '@/components/ui/separator'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const router = useRouter()
const route = useRoute()
const form = reactive({ username: 'ops', password: 'ops123' })
const loading = ref(false)
const showPassword = ref(false)
const ACCOUNTS = [
  { username: 'ops', password: 'ops123', role: '客服', description: '处理工单、回复客户' },
  { username: 'reviewer', password: 'reviewer123', role: '售后主管', description: '审核敏感动作、质检抽查' },
  { username: 'pm', password: 'pm123', role: '产品经理', description: '查看业务分析与诊断' },
  { username: 'admin', password: 'admin123', role: '管理员', description: '管理账号和系统设置' },
  { username: 'eng', password: 'eng123', role: '研发', description: '维护系统和诊断能力' },
]
async function submit(): Promise<void> {
  if (loading.value) return
  loading.value = true
  try {
    await session.login(form.username, form.password)
    ElMessage.success(`欢迎，${session.displayName}`)
    await router.push((route.query.redirect as string) || '/')
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { loading.value = false }
}
function pick(account: (typeof ACCOUNTS)[number]): void {
  form.username = account.username
  form.password = account.password
}
</script>

<template>
  <div class="login-page">
    <section class="login-story" aria-label="产品介绍">
      <div class="login-brand"><span class="login-brand__icon"><Layers :size="22" /></span><span>Product Insight <strong>Copilot</strong></span></div>
      <div class="login-story__body">
        <span class="login-kicker">电商售后 · 多 Agent 协作</span>
        <h1>让每一张工单，<br />都有据可依。</h1>
        <p>把核实订单、查找政策和整理回复交给协作助手，<br class="login-desktop-br" />把判断和确认留在你手中。</p>
        <div class="login-workflow" aria-label="工单处理流程">
          <div><span class="login-step">01</span><span><strong>核实情况</strong><small>核对订单事实，检索适用政策</small></span><Check :size="16" /></div>
          <ChevronRight class="login-workflow__connector" :size="18" />
          <div><span class="login-step">02</span><span><strong>整理建议</strong><small>生成处理方案，独立复核依据</small></span><Check :size="16" /></div>
          <ChevronRight class="login-workflow__connector" :size="18" />
          <div><span class="login-step">03</span><span><strong>确认后回复</strong><small>敏感建议先审批，处理过程可追踪</small></span><ShieldCheck :size="17" /></div>
        </div>
      </div>
      <div class="login-story__footer"><ShieldCheck :size="15" />权限约束 · 人工确认 · 过程留痕</div>
    </section>
    <main class="login-main">
      <div class="login-form-wrap">
        <div class="login-mobile-brand"><Layers :size="20" />Product Insight Copilot</div>
        <div class="pic-eyebrow">WORKSPACE</div>
        <h2>电商售后工作台</h2>
        <p class="login-subtitle">登录工作空间，开始处理今天的工单。</p>
        <form class="login-form" @submit.prevent="submit">
          <div class="login-field"><label for="login-username">账号</label><Input id="login-username" v-model="form.username" autocomplete="username" placeholder="请输入用户名" required :disabled="loading" /></div>
          <div class="login-field">
            <label for="login-password">口令</label>
            <div class="login-password">
              <Input id="login-password" v-model="form.password" :type="showPassword ? 'text' : 'password'" autocomplete="current-password" placeholder="请输入口令" required :disabled="loading" />
              <Button type="button" variant="ghost" size="icon-sm" :aria-label="showPassword ? '隐藏口令' : '显示口令'" :aria-pressed="showPassword" @click="showPassword = !showPassword"><EyeOff v-if="showPassword" :size="15" /><Eye v-else :size="15" /></Button>
            </div>
          </div>
          <Button type="submit" class="login-submit" :disabled="loading"><LoaderCircle v-if="loading" class="animate-spin" :size="16" />{{ loading ? '登录中…' : '登录' }}<ArrowRight v-if="!loading" :size="16" /></Button>
        </form>
        <div class="login-demo-divider"><Separator /><span>演示账号（虚构数据）</span><Separator /></div>
        <div class="login-accounts">
          <button v-for="account in ACCOUNTS" :key="account.username" type="button" class="login-account" :class="{ 'is-selected': form.username === account.username }" :aria-pressed="form.username === account.username" :disabled="loading" @click="pick(account)">
            <span><strong>{{ account.role }}</strong><small>{{ account.description }}</small></span><Check v-if="form.username === account.username" :size="15" /><ChevronRight v-else :size="15" />
          </button>
        </div>
        <p class="login-demo-hint">选择角色会填入演示账号，点击登录进入对应工作区。</p>
      </div>
      <footer class="login-footer">Product Insight Copilot <span>·</span> 电商售后多 Agent 工单系统</footer>
    </main>
  </div>
</template>

<style scoped>
.login-page { min-height: 100dvh; display: grid; grid-template-columns: 1fr 1fr; background: #fff; }
.login-story { display: flex; flex-direction: column; padding: 38px 48px 28px; color: #fafafa; background: #19191d; }
.login-brand { display: flex; align-items: center; gap: 12px; font-size: 16px; letter-spacing: -.3px; }
.login-brand strong { color: #a1a1aa; font-weight: 400; }
.login-brand__icon { display: flex; align-items: center; justify-content: center; width: 37px; height: 37px; background: #303035; border: 1px solid #414147; border-radius: 9px; }
.login-story__body { width: 100%; max-width: 440px; margin: auto; padding: 65px 0; }
.login-kicker { color: #b4b4bc; font-size: 12px; letter-spacing: 1.5px; }
.login-story h1 { margin: 22px 0; font-size: clamp(30px, 3.3vw, 48px); font-weight: 550; line-height: 1.45; letter-spacing: -1.8px; }
.login-story__body > p { font-size: 13px; line-height: 1.9; color: #a1a1aa; }
.login-workflow { margin-top: 38px; }
.login-workflow > div { display: flex; align-items: center; gap: 14px; padding: 14px 16px; border: 1px solid #37373d; border-radius: 8px; background: #222226; }
.login-step { display: inline-flex; font-size: 12px; color: #a1a1aa; }
.login-workflow > div > span:nth-child(2) { display: flex; flex: 1; flex-direction: column; gap: 5px; }
.login-workflow strong { font-size: 12px; font-weight: 500; }
.login-workflow small { font-size: 12px; color: #a1a1aa; }
.login-workflow > div > svg { color: #84ae99; }
.login-workflow__connector { display: block; margin: 6px 0 6px 22px; transform: rotate(90deg); color: #71717a; }
.login-story__footer { display: flex; gap: 8px; align-items: center; color: #a1a1aa; font-size: 12px; }
.login-main { min-width: 0; display: flex; flex-direction: column; align-items: center; padding: 55px 40px 22px; }
.login-form-wrap { width: 100%; max-width: 360px; margin: auto 0; padding: 20px 0 36px; }
.login-form-wrap h2 { margin: 0 0 10px; font-size: 26px; font-weight: 650; letter-spacing: -1px; }
.login-subtitle { font-size: 12px; color: #71717a; line-height: 1.7; margin: 0 0 30px; }
.login-form { display: flex; flex-direction: column; gap: 20px; }
.login-field { display: flex; flex-direction: column; gap: 9px; }
.login-field label { font-size: 12px; font-weight: 500; }
.login-field :deep(input) { height: 40px; font-size: 13px; }
.login-password { position: relative; }
.login-password :deep(input) { padding-right: 42px; }
.login-password > button { position: absolute; top: 4px; right: 4px; color: #71717a; }
.login-submit[data-slot] { width: 100%; height: 40px; margin-top: 2px; font-size: 12px; justify-content: center; }
.login-demo-divider { display: flex; align-items: center; gap: 12px; margin: 27px 0 17px; }
.login-demo-divider span { flex: 0 0 auto; color: #71717a; font-size: 12px; }
.login-demo-divider :deep([data-slot]) { flex: 1; width: auto; }
.login-accounts { display: grid; gap: 6px; }
.login-account { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 9px 12px; background: #fff; border: 1px solid #e4e4e7; border-radius: 6px; color: #71717a; text-align: left; cursor: pointer; }
.login-account > span { display: flex; align-items: baseline; gap: 10px; }
.login-account strong { font-size: 12px; color: #3f3f46; font-weight: 500; }
.login-account small { font-size: 12px; color: #71717a; }
.login-account.is-selected { border-color: #a1a1aa; background: #f7f7f8; color: #18181b; }
.login-account:hover { background: #fafafa; }
.login-account:disabled { cursor: not-allowed; opacity: .6; }
.login-demo-hint { margin: 12px 0 0; color: #71717a; font-size: 12px; line-height: 1.6; }
.login-footer { font-size: 12px; color: #71717a; text-align: center; line-height: 1.7; }
.login-footer span { margin: 0 6px; }
.login-mobile-brand { display: none; }
@media (max-width: 1000px) { .login-story { padding: 30px; } .login-main { padding: 40px 30px 22px; } }
@media (max-width: 760px) { .login-page { grid-template-columns: 1fr; } .login-story { display: none; } .login-main { min-height: 100dvh; padding: 30px 24px 22px; } .login-form-wrap { padding-top: 0; } .login-mobile-brand { display: flex; align-items: center; gap: 9px; margin-bottom: 42px; font-size: 12px; font-weight: 600; } .login-footer { font-size: 12px; } }
</style>

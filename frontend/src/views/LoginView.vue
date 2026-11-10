<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const router = useRouter()
const route = useRoute()

const form = reactive({ username: 'pm', password: 'pm123' })
const loading = ref(false)

const ACCOUNTS = [
  { username: 'pm', password: 'pm123', role: '产品经理（查看业务分析）' },
  { username: 'reviewer', password: 'reviewer123', role: '售后主管（审核退款等敏感动作）' },
  { username: 'admin', password: 'admin123', role: '管理员（管理账号和系统设置）' },
  { username: 'ops', password: 'ops123', role: '客服（处理工单、回复客户）' },
  { username: 'eng', password: 'eng123', role: '研发（维护系统和诊断能力）' },
]

async function submit(): Promise<void> {
  loading.value = true
  try {
    await session.login(form.username, form.password)
    ElMessage.success(`欢迎，${session.displayName}`)
    const redirect = (route.query.redirect as string) || '/'
    await router.push(redirect)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

function pick(account: (typeof ACCOUNTS)[number]): void {
  form.username = account.username
  form.password = account.password
}
</script>

<template>
  <div style="max-width: 460px; margin: 8vh auto 0">
    <el-card shadow="always">
      <template #header>
        <div style="text-align: center">
          <h2 style="margin: 0 0 4px">电商售后工作台</h2>
          <div class="pic-muted">查看工单、核对订单、生成回复；需要审批的动作交给主管</div>
        </div>
      </template>

      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="账号" for="login-username">
          <el-input id="login-username" v-model="form.username" autocomplete="username" placeholder="用户名" />
        </el-form-item>
        <el-form-item label="口令" for="login-password">
          <el-input
            id="login-password"
            v-model="form.password"
            type="password"
            autocomplete="current-password"
            placeholder="口令"
            show-password
          />
        </el-form-item>
        <el-button native-type="submit" type="primary" :loading="loading" style="width: 100%">
          登录
        </el-button>
      </el-form>

      <el-divider>演示账号（虚构数据）</el-divider>
      <div style="display: flex; flex-direction: column; gap: 6px">
        <el-button
          v-for="account in ACCOUNTS"
          :key="account.username"
          size="small"
          text
          style="justify-content: flex-start"
          @click="pick(account)"
        >
          <span class="pic-mono">{{ account.username }} / {{ account.password }}</span>
          <span class="pic-muted" style="margin-left: 8px">{{ account.role }}</span>
        </el-button>
      </div>
    </el-card>
  </div>
</template>

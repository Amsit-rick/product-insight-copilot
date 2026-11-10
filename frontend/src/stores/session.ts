/** 会话状态：当前用户、权限动作与可见知识库。JWT 由 HttpOnly Cookie 管理。 */

import { defineStore } from 'pinia'
import { authApi } from '@/api/endpoints'
import type { MeOut } from '@/api/types'

interface SessionState {
  me: MeOut | null
  loading: boolean
}

export const useSessionStore = defineStore('session', {
  state: (): SessionState => ({
    me: null,
    loading: false,
  }),

  getters: {
    loggedIn: (state): boolean => Boolean(state.me),
    displayName: (state): string => state.me?.full_name || state.me?.username || '未登录',
    roles: (state): string[] => state.me?.roles ?? [],
    actions: (state): string[] => state.me?.actions ?? [],
    knowledgeBases: (state): string[] => state.me?.knowledge_bases ?? [],
  },

  actions: {
    /** 能否执行某个动作（后端是唯一裁决方，这里只用于隐藏入口） */
    can(action: string): boolean {
      return this.actions.includes(action)
    },

    async login(username: string, password: string): Promise<void> {
      await authApi.createSession(username, password)
      await this.loadMe()
    },

    async loadMe(): Promise<void> {
      this.loading = true
      try {
        this.me = await authApi.me()
      } catch (error) {
        this.me = null
        throw error
      } finally {
        this.loading = false
      }
    },

    async logout(): Promise<void> {
      try {
        await authApi.logout()
      } catch {
        // 退出必须幂等：后端会话已过期时仍清理前端状态。
      }
      this.me = null
    },
  },
})

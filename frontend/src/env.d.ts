/// <reference types="vite/client" />

/**
 * `*.vue` 的模块声明由 vue-tsc 的 Volar 插件解析，这里不能再用
 * `declare module '*.vue'` 把它降级成通用组件，否则 SFC 内部类型不会被检查。
 *
 * `process`：仅 vite.config.ts 读取环境变量用（未装 @types/node）。
 */
declare const process: {
  env: Record<string, string | undefined>
  platform: string
}

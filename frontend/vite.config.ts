import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

/**
 * 开发期通过 Vite 代理访问后端，生产构建产物由 FastAPI 直接托管（同源，无需 CORS）。
 * 后端默认端口 8010（本机 8000 常被其他项目占用，这里默认错开）。
 *
 * 注意：这里不使用 node:url / path 等 Node API，从而不需要 @types/node（本机离线装不了）。
 */
const BACKEND = process.env.VITE_BACKEND ?? 'http://127.0.0.1:8010'
const srcDir = new URL('./src', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')

export default defineConfig({
  // Element Plus 按需引入：模板里的 el-* 组件自动注册，避免把整库打进主 bundle。
  // 样式继续使用全局 index.css（ElMessage/ElMessageBox 等服务式 API 也依赖它）。
  plugins: [
    vue(),
    Components({
      dts: false,
      resolvers: [ElementPlusResolver({ importStyle: false })],
    }),
  ],
  resolve: {
    alias: {
      '@': srcDir,
    },
  },
  server: {
    host: '127.0.0.1',
    // 默认 5180：本机 5173 被另一个项目（data_query_agent）的开发服务器占用；
    // strictPort=true 让端口冲突直接报错，而不是悄悄换端口导致"代理指向了别人的后端"。
    port: Number(process.env.VITE_PORT ?? 5180),
    strictPort: true,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/health': { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
})

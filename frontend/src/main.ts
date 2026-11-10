import { createApp } from 'vue'
import { createPinia } from 'pinia'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import './styles/main.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)

for (const [name, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, component)
}

app.mount('#app')

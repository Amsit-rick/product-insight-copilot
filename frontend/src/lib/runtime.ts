import type { InjectionKey, Ref } from 'vue'

export interface RuntimeHealth {
  database: string
  vector_backend: string
  llm: { enabled: boolean; model: string }
}

export const runtimeHealthKey: InjectionKey<Ref<RuntimeHealth | null>> = Symbol('runtime-health')

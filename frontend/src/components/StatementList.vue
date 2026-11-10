<script setup lang="ts">
/** 陈述列表：事实 / 推断 / 假设 / 建议共用一套渲染，只是附加字段不同。 */
import { computed } from 'vue'
import CitationMarks from './CitationMarks.vue'
import type { ReportStatement } from '@/api/types'

const props = defineProps<{
  title: string
  statements: ReportStatement[]
  marks: Record<string, string>
  kind?: 'fact' | 'inference' | 'hypothesis' | 'recommendation'
  emptyText?: string
}>()

const showSource = computed(() => props.kind === 'fact' || props.kind === 'inference')
</script>

<template>
  <el-card shadow="never" class="pic-section-gap">
    <template #header>
      <div style="display: flex; align-items: center; justify-content: space-between">
        <span style="font-weight: 600">{{ title }}</span>
        <span class="pic-muted">{{ statements.length }} 条</span>
      </div>
    </template>

    <el-empty v-if="!statements.length" :description="emptyText ?? '暂无内容'" :image-size="60" />

    <div v-for="(item, index) in statements" :key="item.id ?? index" class="pic-statement">
      <div class="pic-statement__head">
        <el-tag size="small" effect="plain" type="info">{{ item.id ?? `#${index + 1}` }}</el-tag>
        <el-tag v-if="showSource && item.source_agent" size="small" effect="plain">
          {{ item.source_agent }}
        </el-tag>
        <el-tag v-if="typeof item.confidence === 'number'" size="small" effect="plain" type="info">
          置信度 {{ (item.confidence * 100).toFixed(0) }}%
        </el-tag>
        <el-tag v-if="item.priority" size="small" type="warning">{{ item.priority }}</el-tag>
        <el-tag v-if="item.owner_role" size="small" effect="plain">{{ item.owner_role }}</el-tag>
      </div>

      <div class="pic-statement__text">
        {{ item.text }}<CitationMarks :refs="item.evidence_refs" :marks="marks" />
      </div>

      <div v-if="item.basis" class="pic-muted" style="margin-top: 4px">依据：{{ item.basis }}</div>
      <div v-if="item.verify_by" class="pic-muted" style="margin-top: 4px">
        验证方式：{{ item.verify_by }}
      </div>
    </div>
  </el-card>
</template>

<style scoped>
.pic-statement {
  padding: 10px 0;
  border-bottom: 1px dashed #ebeef5;
}
.pic-statement:last-child {
  border-bottom: none;
}
.pic-statement__head {
  display: flex;
  gap: 6px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}
.pic-statement__text {
  line-height: 1.75;
  font-size: 14px;
}
</style>

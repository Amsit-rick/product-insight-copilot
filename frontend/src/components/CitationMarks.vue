<script setup lang="ts">
/**
 * 引用标记：把 statement.evidence_refs（E1/E12…）渲染成 [1] [2]，
 * 鼠标悬停显示来源（文件名/页码/版本或指标+数据版本）。
 */
import { computed } from 'vue'

const props = defineProps<{
  refs?: string[]
  marks: Record<string, string>
}>()

const items = computed(() =>
  (props.refs ?? []).map((ref) => ({
    ref,
    label: (props.marks[ref] ?? `[${ref}]`).split(' ')[0],
    tooltip: props.marks[ref] ?? ref,
  })),
)
</script>

<template>
  <span>
    <el-tooltip v-for="item in items" :key="item.ref" :content="item.tooltip" placement="top">
      <span class="pic-citation">{{ item.label }}</span>
    </el-tooltip>
  </span>
</template>

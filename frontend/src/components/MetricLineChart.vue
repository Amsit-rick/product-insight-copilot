<script setup lang="ts">
/**
 * 指标趋势图：手写内联 SVG。
 *
 * 为什么不引图表库：本机 npm 不可用（离线），而指标趋势只需要一条折线 +
 * 少量坐标标注，SVG 足够且零依赖；后续接入 ECharts 时替换这一个组件即可。
 */
import { computed } from 'vue'

const props = defineProps<{
  points: Array<{ label: string; value: number }>
  height?: number
  color?: string
  valueFormat?: (value: number) => string
}>()

const WIDTH = 720
const PADDING = { top: 16, right: 16, bottom: 26, left: 52 }

const height = computed(() => props.height ?? 200)
const innerWidth = computed(() => WIDTH - PADDING.left - PADDING.right)
const innerHeight = computed(() => height.value - PADDING.top - PADDING.bottom)

const bounds = computed(() => {
  const values = props.points.map((p) => p.value)
  if (!values.length) return { min: 0, max: 1 }
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || Math.abs(max) || 1
  return { min: min - span * 0.12, max: max + span * 0.12 }
})

function x(index: number): number {
  if (props.points.length <= 1) return PADDING.left + innerWidth.value / 2
  return PADDING.left + (innerWidth.value * index) / (props.points.length - 1)
}

function y(value: number): number {
  const { min, max } = bounds.value
  const ratio = (value - min) / (max - min || 1)
  return PADDING.top + innerHeight.value * (1 - ratio)
}

const polyline = computed(() =>
  props.points.map((point, index) => `${x(index).toFixed(1)},${y(point.value).toFixed(1)}`).join(' '),
)

const format = computed(() => props.valueFormat ?? ((value: number) => value.toFixed(4)))

const ticks = computed(() => {
  const { min, max } = bounds.value
  return [max, (max + min) / 2, min].map((value) => ({ value, y: y(value) }))
})

const labelIndexes = computed(() => {
  const total = props.points.length
  if (total <= 1) return [0]
  return [0, Math.floor((total - 1) / 2), total - 1]
})
</script>

<template>
  <div v-if="!points.length" class="pic-muted">没有可绘制的数据点</div>
  <svg v-else :viewBox="`0 0 ${WIDTH} ${height}`" style="width: 100%; height: auto">
    <line
      v-for="tick in ticks"
      :key="`grid-${tick.value}`"
      :x1="PADDING.left"
      :x2="WIDTH - PADDING.right"
      :y1="tick.y"
      :y2="tick.y"
      stroke="#ebeef5"
      stroke-width="1"
    />
    <text
      v-for="tick in ticks"
      :key="`label-${tick.value}`"
      :x="PADDING.left - 8"
      :y="tick.y + 4"
      text-anchor="end"
      font-size="11"
      fill="#909399"
    >
      {{ format(tick.value) }}
    </text>

    <polyline
      :points="polyline"
      fill="none"
      :stroke="color ?? '#409eff'"
      stroke-width="2"
      stroke-linejoin="round"
    />
    <circle
      v-for="(point, index) in points"
      :key="`dot-${index}`"
      :cx="x(index)"
      :cy="y(point.value)"
      r="3"
      :fill="color ?? '#409eff'"
    >
      <title>{{ point.label }}：{{ format(point.value) }}</title>
    </circle>

    <text
      v-for="index in labelIndexes"
      :key="`xlabel-${index}`"
      :x="x(index)"
      :y="height - 8"
      text-anchor="middle"
      font-size="11"
      fill="#909399"
    >
      {{ points[index]?.label?.slice(5) ?? '' }}
    </text>
  </svg>
</template>

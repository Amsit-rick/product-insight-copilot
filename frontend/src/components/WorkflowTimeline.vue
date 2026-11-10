<script setup lang="ts">
/**
 * 工作流时间线：把后端返回的节点序列 + 任务事件流合成"走到哪一步了"。
 * 节点状态由事件推导：已出现完成事件→完成；当前节点→进行中；否则→未开始。
 */
import { computed } from 'vue'
import type { TaskEventOut } from '@/api/types'

const props = defineProps<{
  nodes: string[]
  events: TaskEventOut[]
  currentNode?: string | null
  status: string
}>()

const NODE_LABELS: Record<string, string> = {
  validate_request: '参数与权限校验',
  classify_intent: '意图识别',
  create_plan: '生成受限执行计划',
  product_agent: '产品知识 Agent',
  feedback_agent: '用户反馈 Agent',
  release_agent: '版本研发 Agent',
  metric_agent: '指标解读 Agent',
  validate_agent_outputs: 'Agent 输出校验',
  merge_evidence: '证据合并与冲突检测',
  verify_evidence: '证据充分性与风险判定',
  retrieve_more: '补充检索',
  resolve_conflict: '冲突裁决',
  human_approval: '人工审核',
  generate_report: '生成报告',
  validate_report: '报告校验',
  persist_result: '报告落库',
}

interface NodeState {
  node: string
  label: string
  state: 'done' | 'active' | 'pending'
  message?: string
  level?: string
  elapsed?: string
}

const lastEventByNode = computed(() => {
  const map = new Map<string, TaskEventOut>()
  const doneNodes = new Set<string>()
  for (const event of props.events) {
    if (!event.node) continue
    map.set(event.node, event)
    if (event.level !== 'error') doneNodes.add(event.node)
  }
  return { map, doneNodes }
})

const states = computed<NodeState[]>(() =>
  props.nodes.map((node) => {
    const event = lastEventByNode.value.map.get(node)
    let state: NodeState['state'] = 'pending'
    if (event && event.level !== 'error') state = 'done'
    if (props.status === 'running' && props.currentNode === node) state = 'active'
    if (props.status === 'waiting_human' && node === 'human_approval') state = 'active'
    if (event && event.level === 'error') state = 'pending'
    return {
      node,
      label: NODE_LABELS[node] ?? node,
      state,
      message: event?.message,
      level: event?.level,
      elapsed: event ? new Date(event.created_at).toLocaleTimeString() : undefined,
    }
  }),
)

const finishedCount = computed(() => states.value.filter((s) => s.state === 'done').length)
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div style="display: flex; justify-content: space-between; align-items: center">
        <span style="font-weight: 600">工作流节点</span>
        <span class="pic-muted">已推进 {{ finishedCount }} / {{ states.length }} 个节点</span>
      </div>
    </template>

    <el-timeline>
      <el-timeline-item
        v-for="item in states"
        :key="item.node"
        :type="item.state === 'done' ? 'success' : item.state === 'active' ? 'primary' : 'info'"
        :hollow="item.state === 'pending'"
        :timestamp="item.elapsed"
      >
        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
          <strong>{{ item.label }}</strong>
          <el-tag size="small" effect="plain" type="info" class="pic-mono">{{ item.node }}</el-tag>
          <el-tag v-if="item.state === 'active'" size="small" type="primary">进行中</el-tag>
          <el-tag v-if="item.level === 'warn'" size="small" type="warning">告警</el-tag>
          <el-tag v-if="item.level === 'error'" size="small" type="danger">失败</el-tag>
        </div>
        <div v-if="item.message" class="pic-muted" style="margin-top: 2px">{{ item.message }}</div>
      </el-timeline-item>
    </el-timeline>
  </el-card>
</template>

<script setup lang="ts">
/** 证据面板：文档证据显示来源/页码/版本，指标证据显示能力/数据版本/质量标记。 */
import type { ReportEvidence } from '@/api/types'

defineProps<{
  evidence: ReportEvidence[]
  marks?: Record<string, string>
}>()

const KB_LABELS: Record<string, string> = {
  product_kb: '产品知识库',
  feedback_kb: '用户反馈库',
  engineering_kb: '研发知识库',
  operation_kb: '运营知识库',
}
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div style="display: flex; justify-content: space-between; align-items: center">
        <span style="font-weight: 600">证据清单</span>
        <span class="pic-muted">{{ evidence.length }} 条（全部通过权限过滤）</span>
      </div>
    </template>

    <el-empty v-if="!evidence.length" description="本次没有可用证据" :image-size="60" />

    <el-collapse>
      <el-collapse-item v-for="(item, index) in evidence" :key="item.evidence_id ?? index">
        <template #title>
          <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
            <el-tag size="small" effect="dark">{{ marks?.[item.evidence_id]?.split(' ')[0] ?? item.evidence_id }}</el-tag>
            <el-tag size="small" :type="item.source_kind === 'metric' ? 'warning' : 'success'" effect="plain">
              {{ item.source_kind === 'metric' ? '指标' : '文档' }}
            </el-tag>
            <strong style="font-weight: 500">{{ item.source_name || '未知来源' }}</strong>
            <span v-if="item.page" class="pic-muted">第 {{ item.page }} 页</span>
            <span v-if="item.document_version" class="pic-muted">版本 {{ item.document_version }}</span>
            <el-tag v-if="item.knowledge_base" size="small" effect="plain" type="info">
              {{ KB_LABELS[item.knowledge_base] ?? item.knowledge_base }}
            </el-tag>
            <el-tag v-if="typeof item.relevance_score === 'number'" size="small" effect="plain" type="info">
              相关度 {{ item.relevance_score.toFixed(3) }}
            </el-tag>
          </div>
        </template>

        <div class="pic-evidence-content">{{ item.content || '（无内容摘要）' }}</div>

        <el-descriptions v-if="item.source_kind === 'metric'" :column="2" size="small" class="pic-section-gap">
          <el-descriptions-item label="指标">{{ item.metric_name }}</el-descriptions-item>
          <el-descriptions-item label="能力">{{ item.capability }}</el-descriptions-item>
          <el-descriptions-item label="数据版本">{{ item.dataset_version || '—' }}</el-descriptions-item>
          <el-descriptions-item label="数据来源">{{ item.source_version || '—' }}</el-descriptions-item>
          <el-descriptions-item label="查询参数" :span="2">
            <span class="pic-mono">{{ JSON.stringify(item.params ?? {}) }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <div v-if="(item.quality_flags ?? []).length || (item.warnings ?? []).length" class="pic-section-gap">
          <el-alert
            v-if="(item.quality_flags ?? []).length"
            type="warning"
            :closable="false"
            :title="`数据质量标记：${(item.quality_flags ?? []).join('、')}`"
          />
          <el-alert
            v-for="warning in item.warnings ?? []"
            :key="warning"
            type="info"
            :closable="false"
            :title="warning"
            style="margin-top: 6px"
          />
        </div>
      </el-collapse-item>
    </el-collapse>
  </el-card>
</template>

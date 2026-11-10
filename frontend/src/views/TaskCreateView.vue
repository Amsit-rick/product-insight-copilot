<script setup lang="ts">
/** 新建诊断：问题描述、产品、平台、版本、时间范围、知识库与数据范围。 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { metricApi, taskApi, type TaskCreatePayload } from '@/api/endpoints'
import type { MetricDefinitionOut } from '@/api/types'
import { useSessionStore } from '@/stores/session'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()

const form = reactive<TaskCreatePayload>({
  query: '',
  product: '星野社区',
  platform: 'Android',
  app_version: '3.2.0',
  channel: '',
  device: '',
  date_from: '2026-08-01',
  date_to: '2026-08-31',
  knowledge_bases: [],
  metrics: [],
  priority: 'P1',
  force_human_review: false,
})

const metrics = ref<MetricDefinitionOut[]>([])
const submitting = ref(false)

const KB_OPTIONS = [
  { value: 'product_kb', label: '产品知识库（PRD / 规则 / 埋点字典）' },
  { value: 'feedback_kb', label: '用户反馈库（工单 / 评论 / 访谈）' },
  { value: 'engineering_kb', label: '研发知识库（发布记录 / 故障复盘）' },
  { value: 'operation_kb', label: '运营知识库（指标定义 / 实验记录）' },
]

const availableKbs = computed(() =>
  KB_OPTIONS.filter((option) => session.knowledgeBases.includes(option.value)),
)

const canSubmit = computed(() => form.query.trim().length >= 6)

const SAMPLE = {
  query:
    'Android 端 3.2.0 发布后次日留存下降，哪些用户受到影响？版本变更、用户反馈和故障记录是否支持版本相关性？下一步应该优先验证什么？',
}

onMounted(async () => {
  form.knowledge_bases = [...session.knowledgeBases]
  // 从「知识库问答」页点「转成完整诊断」跳转过来时，带上原问题
  const preset = route.query.q
  if (typeof preset === 'string' && preset.trim().length > 0) {
    form.query = preset.trim()
    ElMessage.info('已带入知识库问答中的问题，可继续补充平台/版本/指标后发起诊断')
  }
  try {
    metrics.value = await metricApi.definitions()
    form.metrics = metrics.value.map((item) => item.name).slice(0, 2)
  } catch (error) {
    ElMessage.warning(`指标字典加载失败：${(error as Error).message}`)
  }
})

async function submit(): Promise<void> {
  if (!canSubmit.value) {
    ElMessage.warning('问题描述至少 6 个字')
    return
  }
  submitting.value = true
  try {
    const payload: TaskCreatePayload = { ...form }
    if (!payload.channel) delete payload.channel
    if (!payload.device) delete payload.device
    const task = await taskApi.create(payload)
    ElMessage.success('任务已创建，正在执行工作流')
    await router.push({ name: 'task-progress', params: { id: task.id } })
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div>
    <div class="pic-page-title">
      <h2>新建诊断</h2>
      <el-button link type="primary" @click="router.push({ name: 'workbench' })">返回工作台</el-button>
    </div>

    <el-alert
      type="info"
      :closable="false"
      title="说明：这里填写的范围会原样进入受限执行计划——平台/版本/渠道/机型都会带进指标查询，知识库范围受你的角色权限约束。"
      class="pic-section-gap"
    />

    <el-card shadow="never" class="pic-section-gap">
      <el-form label-width="120px" label-position="top">
        <el-form-item label="问题描述">
          <el-input
            v-model="form.query"
            type="textarea"
            :rows="4"
            maxlength="2000"
            show-word-limit
            placeholder="例如：Android 端 3.2.0 发布后次日留存下降，哪些用户受到影响？下一步应该优先验证什么？"
          />
          <el-button link type="primary" class="task-sample-button" @click="form.query = SAMPLE.query">
            填入示例问题
          </el-button>
        </el-form-item>

        <el-row :gutter="16">
          <el-col :span="6">
            <el-form-item label="产品">
              <el-input v-model="form.product" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="平台">
              <el-select v-model="form.platform" style="width: 100%">
                <el-option label="Android" value="Android" />
                <el-option label="iOS" value="iOS" />
                <el-option label="全部" value="" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="版本">
              <el-input v-model="form.app_version" placeholder="如 3.2.0" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="优先级">
              <el-select v-model="form.priority" style="width: 100%">
                <el-option v-for="p in ['P0', 'P1', 'P2', 'P3']" :key="p" :label="p" :value="p" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="16">
          <el-col :span="6">
            <el-form-item label="渠道（可选）">
              <el-input v-model="form.channel" placeholder="如 channel_a" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="机型档位（可选）">
              <el-input v-model="form.device" placeholder="如 low_end" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="开始日期">
              <el-date-picker
                v-model="form.date_from"
                type="date"
                value-format="YYYY-MM-DD"
                style="width: 100%"
              />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="结束日期">
              <el-date-picker
                v-model="form.date_to"
                type="date"
                value-format="YYYY-MM-DD"
                style="width: 100%"
              />
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="知识库范围（只显示你有权限的）">
          <el-checkbox-group v-model="form.knowledge_bases">
            <el-checkbox v-for="option in availableKbs" :key="option.value" :value="option.value">
              {{ option.label }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>

        <el-form-item label="指标范围">
          <el-select v-model="form.metrics" multiple style="width: 100%" placeholder="选择要查询的指标">
            <el-option
              v-for="metric in metrics"
              :key="metric.name"
              :label="`${metric.display_name}（${metric.name}）`"
              :value="metric.name"
            />
          </el-select>
        </el-form-item>

        <el-form-item>
          <el-checkbox v-model="form.force_human_review">
            无论风险等级，都先转人工审核再出报告
          </el-checkbox>
        </el-form-item>

        <el-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit">
          创建并执行诊断
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
/** 知识库管理：文档上传、版本、索引状态、权限与失败记录 + 指标数据导入。 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadFile, UploadFiles } from 'element-plus'
import { knowledgeApi, metricApi } from '@/api/endpoints'
import type {
  DocumentOut,
  KnowledgeBaseOut,
  MetricDefinitionOut,
  MetricImportResponse,
} from '@/api/types'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

const kbs = ref<KnowledgeBaseOut[]>([])
const documents = ref<DocumentOut[]>([])
const definitions = ref<MetricDefinitionOut[]>([])
const total = ref(0)
const loading = ref(false)
const uploading = ref(false)
const importing = ref(false)
const importResult = ref<MetricImportResponse | null>(null)

const filters = reactive({ kb_code: '', status: '', page: 1, page_size: 10 })

const uploadForm = reactive({
  kb_code: '',
  name: '',
  document_version: 'v1',
  security_level: 'internal',
  department: '',
  app_version: '',
  doc_updated_at: '',
})

const metricFile = ref<File | null>(null)
const metricDatasetVersion = ref('')

const canUpload = computed(() => session.can('document:upload'))
const canDelete = computed(() => session.can('document:delete'))
const canReindex = computed(() => session.can('kb:reindex'))
const canImportMetric = computed(() => session.can('metric:import'))

const STATUS_LABEL: Record<string, string> = {
  uploaded: '已上传',
  parsing: '解析中',
  indexing: '索引中',
  indexed: '已索引',
  failed: '失败',
  deleted: '已删除',
}

const STATUS_TYPE: Record<string, 'success' | 'warning' | 'danger' | 'info'> = {
  indexed: 'success',
  parsing: 'warning',
  indexing: 'warning',
  failed: 'danger',
  uploaded: 'info',
  deleted: 'info',
}

async function loadKbs(): Promise<void> {
  kbs.value = await knowledgeApi.list()
  if (!uploadForm.kb_code && kbs.value.length) uploadForm.kb_code = kbs.value[0].code
}

async function loadDocuments(): Promise<void> {
  loading.value = true
  try {
    const result = await knowledgeApi.documents({
      kb_code: filters.kb_code || undefined,
      status: filters.status || undefined,
      page: filters.page,
      page_size: filters.page_size,
    })
    documents.value = result.items
    total.value = result.meta.total
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

async function loadDefinitions(): Promise<void> {
  try {
    definitions.value = await metricApi.definitions()
  } catch {
    definitions.value = []
  }
}

async function loadAll(): Promise<void> {
  await Promise.all([loadKbs(), loadDocuments(), loadDefinitions()])
}

async function submitUpload(files: UploadFiles): Promise<void> {
  if (!files.length) return
  const file = files[0].raw as UploadFile['raw']
  if (!file) return
  uploading.value = true
  const form = new FormData()
  form.append('file', file)
  form.append('kb_code', uploadForm.kb_code)
  if (uploadForm.name) form.append('name', uploadForm.name)
  form.append('document_version', uploadForm.document_version)
  form.append('security_level', uploadForm.security_level)
  if (uploadForm.department) form.append('department', uploadForm.department)
  if (uploadForm.app_version) form.append('app_version', uploadForm.app_version)
  if (uploadForm.doc_updated_at) form.append('doc_updated_at', uploadForm.doc_updated_at)

  try {
    const result = await knowledgeApi.upload(form)
    ElMessage.success(`入库成功：${result.chunks} 个切片，状态 ${result.index_status}`)
    result.warnings.forEach((warning) => ElMessage.warning(warning))
    await loadAll()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    uploading.value = false
  }
}

async function remove(document: DocumentOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除《${document.source_name}》及其 ${document.chunk_count} 个切片和向量？`,
      '删除文档',
      { type: 'warning' },
    )
    const result = await knowledgeApi.remove(document.id)
    ElMessage.success(`已删除，清理切片 ${result.deleted_chunks} 个`)
    await loadAll()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error((error as Error).message)
  }
}

async function reindex(kb: KnowledgeBaseOut): Promise<void> {
  try {
    await ElMessageBox.confirm(`重建「${kb.name}」的索引（重新解析 + 重新计算向量）？`, '重建索引', {
      type: 'warning',
    })
    const result = await knowledgeApi.reindex(kb.code, true)
    ElMessage.success(
      `重建完成：文档 ${result.documents} 篇、切片 ${result.chunks} 个` +
        (result.failed.length ? `，失败 ${result.failed.length} 篇` : ''),
    )
    result.failed.forEach((item) => ElMessage.error(`${item.document_id}：${item.error}`))
    await loadAll()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error((error as Error).message)
  }
}

function onMetricFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  metricFile.value = input.files?.[0] ?? null
}

async function importMetrics(): Promise<void> {
  if (!metricFile.value) {
    ElMessage.warning('请先选择 CSV / XLSX 文件')
    return
  }
  importing.value = true
  const form = new FormData()
  form.append('file', metricFile.value)
  if (metricDatasetVersion.value) form.append('dataset_version', metricDatasetVersion.value)
  try {
    const result = await metricApi.import(form)
    importResult.value = result
    ElMessage.success(
      `导入完成：有效 ${result.rows_valid}/${result.rows_total} 行，状态 ${result.status}`,
    )
    await loadDefinitions()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    importing.value = false
  }
}

onMounted(loadAll)
</script>

<template>
  <div>
    <div class="pic-page-title">
      <h2>知识库管理</h2>
      <el-button @click="loadAll">刷新</el-button>
    </div>

    <el-card shadow="never">
      <template #header><span style="font-weight: 600">知识库概览（仅显示你有权限的）</span></template>
      <el-table :data="kbs" size="small">
        <el-table-column prop="name" label="知识库" width="160" />
        <el-table-column prop="code" label="代码" width="150">
          <template #default="{ row }"><span class="pic-mono">{{ row.code }}</span></template>
        </el-table-column>
        <el-table-column label="密级" width="110">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ row.security_level }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="document_count" label="文档" width="80" />
        <el-table-column prop="chunk_count" label="切片" width="80" />
        <el-table-column label="索引状态" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.index_status === 'indexed' ? 'success' : 'info'">
              {{ row.index_status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近索引" width="170">
          <template #default="{ row }">
            {{ row.last_indexed_at ? new Date(row.last_indexed_at).toLocaleString() : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button v-if="canReindex" link type="primary" @click="reindex(row)">重建索引</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-row :gutter="16" class="pic-section-gap">
      <el-col :span="10">
        <el-card shadow="never">
          <template #header><span style="font-weight: 600">上传文档</span></template>
          <el-alert
            v-if="!canUpload"
            type="warning"
            :closable="false"
            title="当前角色没有 document:upload 权限"
            style="margin-bottom: 10px"
          />
          <el-form label-position="top" :disabled="!canUpload">
            <el-form-item label="目标知识库">
              <el-select v-model="uploadForm.kb_code" aria-label="目标知识库" style="width: 100%">
                <el-option v-for="kb in kbs" :key="kb.code" :label="kb.name" :value="kb.code" />
              </el-select>
            </el-form-item>
            <el-row :gutter="10">
              <el-col :span="12">
                <el-form-item label="文档版本">
                  <el-input v-model="uploadForm.document_version" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="密级">
                  <el-select v-model="uploadForm.security_level" aria-label="文档密级" style="width: 100%">
                    <el-option v-for="level in ['public', 'internal', 'confidential', 'secret']" :key="level" :label="level" :value="level" />
                  </el-select>
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="10">
              <el-col :span="12">
                <el-form-item label="所属部门">
                  <el-input v-model="uploadForm.department" placeholder="如 产品部" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="关联版本">
                  <el-input v-model="uploadForm.app_version" placeholder="如 3.2.0" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="文档更新时间">
              <el-date-picker
                v-model="uploadForm.doc_updated_at"
                type="date"
                value-format="YYYY-MM-DD"
                style="width: 100%"
                placeholder="用于按时间过滤检索"
              />
            </el-form-item>
            <el-form-item label="文件（PDF / Word / Excel / CSV / Markdown）">
              <el-upload
                :auto-upload="false"
                :limit="1"
                :tabindex="-1"
                aria-label="选择知识库文档文件"
                :on-change="(_file: UploadFile, files: UploadFiles) => submitUpload(files)"
                :show-file-list="false"
              >
                <span class="el-button el-button--primary">选择文件并入库</span>
              </el-upload>
              <div class="pic-muted" style="margin-top: 6px">
                上传会经过扩展名白名单、大小上限、文件头魔数与 Office 宏检查；命中注入特征会记告警。
              </div>
            </el-form-item>
          </el-form>
        </el-card>

        <el-card shadow="never" class="pic-section-gap">
          <template #header><span style="font-weight: 600">指标数据导入</span></template>
          <el-alert
            v-if="!canImportMetric"
            type="warning"
            :closable="false"
            title="当前角色没有 metric:import 权限"
            style="margin-bottom: 10px"
          />
          <el-form label-position="top" :disabled="!canImportMetric">
            <el-form-item label="数据集版本（留空则按当前时间生成）">
              <el-input v-model="metricDatasetVersion" placeholder="如 metric_snapshot_20260831" />
            </el-form-item>
            <el-form-item label="CSV / XLSX 文件">
              <input aria-label="指标数据导入文件" type="file" accept=".csv,.xlsx,.xlsm" @change="onMetricFileChange" />
              <div class="pic-muted" style="margin-top: 6px">
                列名支持别名映射（指标名/日期/指标值…）；逐行做口径校验，重复行按「保留最新」处理，
                缺失日期与小样本会写入质量标记。
              </div>
            </el-form-item>
            <el-button type="primary" :loading="importing" @click="importMetrics">开始导入</el-button>
          </el-form>

          <div v-if="importResult" class="pic-section-gap">
            <el-descriptions :column="2" size="small" border>
              <el-descriptions-item label="状态">{{ importResult.status }}</el-descriptions-item>
              <el-descriptions-item label="数据版本">{{ importResult.dataset_version }}</el-descriptions-item>
              <el-descriptions-item label="有效行">{{ importResult.rows_valid }}/{{ importResult.rows_total }}</el-descriptions-item>
              <el-descriptions-item label="重复行">{{ importResult.duplicates }}</el-descriptions-item>
              <el-descriptions-item label="缺失日期" :span="2">
                {{ importResult.missing_days.join(', ') || '无' }}
              </el-descriptions-item>
              <el-descriptions-item label="质量标记" :span="2">
                {{ importResult.quality_flags.join('、') || '无' }}
              </el-descriptions-item>
            </el-descriptions>
            <el-table v-if="importResult.issues.length" :data="importResult.issues" size="small" max-height="240" class="pic-section-gap">
              <el-table-column prop="row" label="行" width="70" />
              <el-table-column label="级别" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.level === 'error' ? 'danger' : row.level === 'warning' ? 'warning' : 'info'">
                    {{ row.level }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="code" label="代码" width="170" />
              <el-table-column prop="message" label="说明" min-width="240" />
            </el-table>
          </div>
        </el-card>
      </el-col>

      <el-col :span="14">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span style="font-weight: 600">文档列表</span>
              <div style="display: flex; gap: 8px">
                <el-select v-model="filters.kb_code" aria-label="按知识库筛选文档" placeholder="全部知识库" clearable style="width: 160px" @change="loadDocuments">
                  <el-option v-for="kb in kbs" :key="kb.code" :label="kb.name" :value="kb.code" />
                </el-select>
                <el-select v-model="filters.status" aria-label="按文档状态筛选" placeholder="全部状态" clearable style="width: 130px" @change="loadDocuments">
                  <el-option v-for="(label, value) in STATUS_LABEL" :key="value" :label="label" :value="value" />
                </el-select>
              </div>
            </div>
          </template>

          <el-table v-loading="loading" :data="documents" size="small">
            <el-table-column label="文档" min-width="200">
              <template #default="{ row }">
                <div style="font-weight: 500">{{ row.name }}</div>
                <div class="pic-muted pic-mono">{{ row.source_name }}</div>
                <div v-if="row.error_message" style="color: #f56c6c; font-size: 12px">
                  {{ row.error_message }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="知识库" width="110">
              <template #default="{ row }">{{ row.kb_code || '—' }}</template>
            </el-table-column>
            <el-table-column label="版本" width="90">
              <template #default="{ row }">{{ row.document_version }}</template>
            </el-table-column>
            <el-table-column label="密级" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="row.security_level === 'confidential' ? 'danger' : 'info'">
                  {{ row.security_level }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="STATUS_TYPE[row.status] ?? 'info'">
                  {{ STATUS_LABEL[row.status] ?? row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="chunk_count" label="切片" width="70" />
            <el-table-column label="更新时间" width="120">
              <template #default="{ row }">{{ row.doc_updated_at || '—' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-button v-if="canDelete" link type="danger" @click="remove(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>

          <el-pagination
            v-model:current-page="filters.page"
            v-model:page-size="filters.page_size"
            :total="total"
            :page-sizes="[10, 20, 50]"
            layout="total, prev, pager, next"
            class="pic-section-gap"
            @current-change="loadDocuments"
            @size-change="loadDocuments"
          />
        </el-card>

        <el-card shadow="never" class="pic-section-gap">
          <template #header><span style="font-weight: 600">指标字典</span></template>
          <el-table :data="definitions" size="small">
            <el-table-column prop="display_name" label="指标" width="140" />
            <el-table-column prop="name" label="代码" width="180">
              <template #default="{ row }"><span class="pic-mono">{{ row.name }}</span></template>
            </el-table-column>
            <el-table-column prop="unit" label="单位" width="80" />
            <el-table-column prop="period" label="周期" width="80" />
            <el-table-column label="允许维度" min-width="200">
              <template #default="{ row }">
                <el-tag v-for="dim in row.allowed_dimensions" :key="dim" size="small" style="margin-right: 4px">
                  {{ dim }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="口径" min-width="220">
              <template #default="{ row }">
                <span class="pic-muted">{{ row.description || '—' }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

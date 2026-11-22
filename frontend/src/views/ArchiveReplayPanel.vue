<template>
  <section class="archive-panel" data-testid="archive-replay-panel">
    <header class="archive-panel-head">
      <div>
        <h3>归档回放证据</h3>
        <p class="page-desc">
          已封存档案与登记状态分开呈现；仅回放索引已确认的档案计入质量统计，未确认文件不会冒充完整结果。
        </p>
      </div>
      <button class="btn" data-testid="trigger-replay" type="button" :disabled="replaying" @click="triggerReplay">
        {{ replaying ? '回放中…' : '回放持久档案' }}
      </button>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">已确认档案</span>
        <strong class="stat-value" data-testid="confirmed-count">{{ evidence?.confirmed_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">未确认文件</span>
        <strong class="stat-value" data-testid="unconfirmed-count">{{ evidence?.unconfirmed_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">归档异常数</span>
        <strong class="stat-value" data-testid="abnormal-count">{{ evidence?.abnormal_count ?? 0 }}</strong>
      </article>
    </div>

    <p v-if="panelError" class="error-text" data-testid="archive-panel-error">{{ panelError }}</p>
    <p v-if="replayMessage" class="replay-message" data-testid="replay-message">{{ replayMessage }}</p>

    <table v-if="archives.length" class="data-table archive-table">
      <thead>
        <tr>
          <th>记录编号</th>
          <th>操作类型</th>
          <th>归档时间</th>
          <th>异常</th>
          <th>确认状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in archives" :key="row.record_id" :data-testid="`archive-row-${row.record_id}`">
          <td>{{ row.record_id }}</td>
          <td>{{ row.operation || '—' }}</td>
          <td>{{ row.archived_at || '—' }}</td>
          <td>{{ row.abnormal ? '是' : '否' }}</td>
          <td :data-confirmed="String(row.confirmed)">
            {{ row.confirmed ? '已确认（计入统计）' : '未确认（不计入）' }}
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="empty-state">暂无持久化审计档案</p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { listAuditArchives, loadArchiveQualityView, replayAuditArchives } from '@/api/backend-service'
import type { ArchivedAudit, ArchiveEvidence } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()

const archives = ref<ArchivedAudit[]>([])
const evidence = ref<ArchiveEvidence | null>(null)
const replaying = ref(false)
const panelError = ref('')
const replayMessage = ref('')

async function loadData() {
  panelError.value = ''
  try {
    if (props.moduleKey === 'audit_log') {
      const payload = await listAuditArchives()
      archives.value = payload.archives
      evidence.value = payload.evidence
    } else {
      const view = await loadArchiveQualityView()
      evidence.value = view
      archives.value = [
        ...view.confirmed_archives.map((item) => ({
          record_id: item.record_id,
          subject: '',
          operation: item.operation,
          target: '',
          archived_at: '',
          abnormal: item.abnormal,
          confirmed: true,
          file: item.file,
        })),
        ...view.unconfirmed_files.map((item) => ({
          record_id: item.record_id,
          subject: '',
          operation: item.operation,
          target: '',
          archived_at: '',
          abnormal: item.abnormal,
          confirmed: false,
          file: item.file,
        })),
      ]
    }
  } catch (error) {
    panelError.value = error instanceof Error ? error.message : '归档证据读取失败'
  }
}

async function triggerReplay() {
  replaying.value = true
  panelError.value = ''
  try {
    const result = await replayAuditArchives()
    replayMessage.value =
      `回放完成：重放 ${result.replayed_count} 条已确认档案，` +
      `${result.unconfirmed_count} 个未确认文件未计入` +
      (result.missing_index_files.length
        ? `，${result.missing_index_files.length} 条登记缺少档案`
        : '')
    await loadData()
  } catch (error) {
    panelError.value = error instanceof Error ? error.message : '回放失败'
  } finally {
    replaying.value = false
  }
}

onMounted(loadData)

defineExpose({ loadData })
</script>

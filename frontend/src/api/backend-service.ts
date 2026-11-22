import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  ArchiveListResult,
  ArchiveQualityView,
  EntryRow,
  OverviewResult,
  PageResult,
  ReplayResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 列出磁盘上全部审计档案及回放索引、确认证据。 */
export async function listAuditArchives(): Promise<ArchiveListResult> {
  const response = await request('/api/audit-log/archives')
  if (!response.ok) throw new Error('审计归档读取失败')
  return await response.json()
}

/** 触发持久化审计档案回放，并同步质量统计。 */
export async function replayAuditArchives(): Promise<ReplayResult> {
  const response = await request('/api/audit-log/replay', { method: 'POST' })
  if (!response.ok) throw new Error('审计归档回放失败')
  return await response.json()
}

/** 回读质量报告中的归档证据（事件级确认/异常状态）。 */
export async function loadArchiveQualityView(): Promise<ArchiveQualityView> {
  const response = await request('/api/quality-report/archive-view')
  if (!response.ok) throw new Error('归档质量证据读取失败')
  return await response.json()
}

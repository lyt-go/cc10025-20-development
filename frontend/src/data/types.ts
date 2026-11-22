/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

/** 已封存审计档案（confirmed 以归档回放索引为准）。 */
export type ArchivedAudit = {
  record_id: string
  subject: string
  operation: string
  target: string
  archived_at: string
  abnormal: boolean
  confirmed: boolean
  file: string
}

/** 归档回放索引中的单条登记。 */
export type ReplayIndexEntry = {
  record_id: string
  file: string
  abnormal: boolean
  confirmed: boolean
  archived_at: string
  indexed_at: string
  replayed?: number
}

export type ArchiveEvidence = {
  generated_at: string
  confirmed_count: number
  unconfirmed_count: number
  abnormal_count: number
  abnormal_record_ids: string[]
  confirmed_archives: {
    record_id: string
    confirmed: boolean
    abnormal: boolean
    operation: string
    file: string
  }[]
  unconfirmed_files: {
    record_id: string
    confirmed: boolean
    abnormal: boolean
    operation: string
    file: string
  }[]
  missing_index_files: string[]
  events: Record<string, {
    record_id: string
    confirmed: boolean
    abnormal: boolean
    operation: string
    file: string
  }>
}

export type ArchiveListResult = {
  archives: ArchivedAudit[]
  replay_index: { version: number; generated_at: string; archives: ReplayIndexEntry[] }
  evidence: ArchiveEvidence
}

export type ReplayResult = {
  replayed_count: number
  replayed_record_ids: string[]
  unconfirmed_count: number
  unconfirmed_record_ids: string[]
  missing_index_files: string[]
}

export type ArchiveQualityView = ArchiveEvidence

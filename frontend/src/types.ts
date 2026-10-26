export type InspectionMaterial = {
  material_group: string
  name: string
  file_name_id: string
  role: string
  cadence: string
}

export type InspectionItem = {
  itemCode: string
  name: string
  handler: string
  enabled: boolean
  name_aliases: string[]
  materials: InspectionMaterial[]
}

export type InspectionConfig = {
  schema_version: number
  query_policy: { month_source: string; fixed_month?: string | null }
  items: InspectionItem[]
}

export type TaskItemSummary = {
  run_id: string
  item_code: string
  item_name?: string
  execution_state: string
  result_status?: string | null
  description?: string | null
  elapsed_ms?: number | null
  latest_event?: ProgressEvent | { sequence?: number; created?: number; event?: ProgressEvent }
  query_scope?: QueryScope
  progress?: TaskProgress
}

export type QueryScope = {
  requested_cycle?: string
  resolved_cycle?: string
  period?: { start?: string; end?: string }
  month_source?: string
  source?: 'frozen_query' | 'accepted_task'
  differs_from_task?: boolean
}

export type TaskProgress = {
  catalog_files?: number
  downloaded?: number
  download_failed?: number
  processed_files?: number
  processing_files?: number
  current_file?: { file_id?: string; file_name?: string; page?: number; page_total?: number } | null
  stage?: string
  latest_event?: ProgressEvent | null
  recognition_pages_completed?: number
  recognition_pages_failed?: number
}

export type TaskSummary = {
  record_id: string
  created: number
  source_system: string
  mine_code: string
  environment: string
  check_cycle: string
  execution_state: string
  item_count: number
  completed_count: number
  items: TaskItemSummary[]
}

export type TaskListResponse = {
  records: TaskSummary[]
  total: number
  offset: number
  limit: number
  has_more: boolean
}

export type TaskDetail = TaskSummary & {
  origin?: string
  elapsed_ms?: number
  request?: Record<string, unknown>
  query_policy?: Record<string, unknown>
}

export type PageResponse<T> = {
  items: T[]
  total: number
  offset: number
  limit: number
  has_more: boolean
}

export type TaskReport = {
  status?: string
  description?: string
  detail?: string
  error_type?: string | null
  error_stage?: string | null
  failure_location?: { file_id: string; file_name?: string | null; page?: number | null } | null
  source_complete?: boolean
  observation?: Record<string, unknown>
  counts?: Record<string, number>
  findings?: Record<string, unknown>[]
  daily_issues?: Record<string, unknown>[]
  finding_count?: number
  daily_issue_count?: number
  rule_coverage?: Record<string, unknown>
}

export type ProgressEvent = {
  sequence?: number
  created?: number
  stage?: string
  event?: string
  status?: string
  file_id?: string
  file_name?: string
  file_index?: number
  file_total?: number
  page?: number
  page_total?: number
  provider?: string
  elapsed_ms?: number
  issues?: string[]
}

import type { InspectionConfig, PageResponse, ProgressEvent, TaskDetail, TaskListResponse, TaskReport } from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { headers: { Accept: 'application/json', ...(init?.headers || {}) }, ...init })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `请求失败 HTTP ${response.status}`)
  return body as T
}

export const api = {
  config: (signal?: AbortSignal) => request<{ config: InspectionConfig; revision: string }>('/v1/debug/inspection-config', { signal }),
  saveConfig: (config: InspectionConfig, expectedRevision: string, signal?: AbortSignal) => request<{ config: InspectionConfig; revision: string }>('/v1/debug/inspection-config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ config, expectedRevision }), signal,
  }),
  tasks: (offset = 0, limit = 20, mine = '', cycle = '', signal?: AbortSignal) => {
    const query = new URLSearchParams({ offset: String(offset), limit: String(limit) })
    if (mine) query.set('mine_code', mine)
    if (cycle) query.set('check_cycle', cycle)
    return request<TaskListResponse>(`/v1/debug/executions?${query}`, { signal })
  },
  task: (recordId: string, signal?: AbortSignal) => request<TaskDetail>(`/v1/debug/tasks/${encodeURIComponent(recordId)}`, { signal }),
  report: (runId: string, signal?: AbortSignal) => request<TaskReport>(`/v1/debug/runs/${encodeURIComponent(runId)}/report`, { signal }),
  files: (runId: string, offset = 0, signal?: AbortSignal) => request<PageResponse<Record<string, unknown>>>(`/v1/debug/runs/${encodeURIComponent(runId)}/files?offset=${offset}&limit=20`, { signal }),
  evidence: (runId: string, offset = 0, filter = '', file = '', signal?: AbortSignal) => {
    const query = new URLSearchParams({offset: String(offset), limit: '20'})
    if (filter) query.set('refs', filter)
    if (file) query.set('file_id', file)
    return request<PageResponse<Record<string, unknown>>>(`/v1/debug/runs/${encodeURIComponent(runId)}/evidence?${query}`, { signal })
  },
  diagnostics: (runId: string, section: string, offset = 0, signal?: AbortSignal) => request<PageResponse<unknown>>(`/v1/debug/runs/${encodeURIComponent(runId)}/diagnostics?section=${encodeURIComponent(section)}&offset=${offset}&limit=20`, { signal }),
  progress: (runId: string, after = 0, signal?: AbortSignal) => request<{ events: ProgressEvent[]; next_after?: number; has_more?: boolean }>(`/v1/debug/runs/${encodeURIComponent(runId)}/progress?after=${after}&limit=100`, { signal }),
  options: (signal?: AbortSignal) => request<{oceanbase_available: boolean; allowed_mines: string[]}>('/v1/debug/task-options', { signal }),
  trigger: (payload: Record<string, unknown>, signal?: AbortSignal) => request<{ task_key?: string; items: { run_id: string; item_code: string }[]; owner?: { sourceSystem: string; mineCode: string; environment: string } }>('/v1/debug/upstream/tasks', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), signal,
  }),
}

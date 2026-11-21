import type { RunFacets, BulkResponse, RetentionPolicy, RunListMeta, AlertRule, AlertRuleInput, ApiKey, ApiResponse, Artifact, CreatedApiKey, CreatedProject, Project, UserAccount, MediaItem, Metric, SampleStep, SamplesPage, ParamStat, ProfileEvent, Run, RunEvent, RunLog, RunSummary, Session, SystemSnapshot } from './types.ts'

async function apiFetchFull<T>(path: string): Promise<{ data: T; meta?: ApiResponse<T>['meta'] }> {
  const res = await fetch(path)
  if (res.status === 401) {
    // Session expired or key revoked: reload so the login gate takes over.
    window.location.reload()
  }
  if (!res.ok) {
    let msg = res.statusText
    try {
      const body = await res.json()
      msg = body?.detail?.error ?? body?.error ?? res.statusText
    } catch {
      // ignore parse error, use statusText
    }
    throw new Error(msg)
  }
  const json: ApiResponse<T> = await res.json()
  if (json.error) throw new Error(json.error)
  return { data: json.data, meta: json.meta }
}

async function apiFetch<T>(path: string): Promise<T> {
  return (await apiFetchFull<T>(path)).data
}

async function authSend<T>(method: 'POST' | 'PATCH' | 'PUT' | 'DELETE', path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const json = await res.json().catch(() => null)
  if (!res.ok) throw new Error(json?.detail?.error ?? json?.error ?? res.statusText)
  return json.data as T
}

export type RunSort = 'updated' | 'created' | 'name'

/** Server-side downsampling / zoom window of the series reads (see docs/server-api.md). */
export interface SeriesQuery {
  max_points?: number
  step_min?: number
  step_max?: number
  t_min?: number
  t_max?: number
}

export interface SeriesResult<T> {
  data: T[]
  /** `downsampled` is true when the server thinned the series. */
  downsampled: boolean
}

export interface RunQuery {
  sort?: RunSort
  limit?: number
  offset?: number
  include_archived?: boolean
  group?: string
  project?: string
  status?: string
  /** Case-insensitive substring of name, group or a tag */
  q?: string
  /** Comma-separated; the run must have all of them */
  tags?: string
  /** Unix seconds or ISO date; created_after inclusive, created_before exclusive */
  created_after?: string | number
  created_before?: string | number
}

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '' && v !== false) p.set(k, String(v))
  }
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const api = {
  getSession: () => apiFetch<Session>('/auth/session'),
  setup: (token: string, username: string, password: string) =>
    authSend<{ user: string }>('POST', '/auth/setup', { token, username, password }),
  login: (username: string, password: string) =>
    authSend<{ user: string }>('POST', '/auth/login', { username, password }),
  logout: () => authSend<{ logged_out: boolean }>('POST', '/auth/logout'),
  changePassword: (current: string, next: string) =>
    authSend<{ changed: boolean }>('POST', '/auth/password', { current, new: next }),

  listProjects: (opts: { include_archived?: boolean; sort?: 'name' } = {}) =>
    apiFetch<Project[]>(`/projects${qs({ ...opts })}`),
  archiveProject: (name: string, archive: boolean) =>
    authSend<{ archived: boolean }>('POST', `/admin/projects/${encodeURIComponent(name)}/${archive ? 'archive' : 'unarchive'}`),
  deleteProject: (name: string, cascade: boolean) =>
    authSend<{ deleted: string; runs_deleted: number; keys_revoked: number; alert_rules_removed: number }>(
      'DELETE',
      `/admin/projects/${encodeURIComponent(name)}?cascade=${cascade}`,
    ),
  setRetention: (name: string, retention: RetentionPolicy | null) =>
    authSend<Project>('PATCH', `/admin/projects/${encodeURIComponent(name)}`, { retention }),
  runRetention: (name: string) =>
    authSend<{ deleted: Record<string, number> }>('POST', `/admin/projects/${encodeURIComponent(name)}/retention/run`),
  projectExportUrl: (name: string) => `/projects/${encodeURIComponent(name)}/export`,
  runExportUrl: (id: string) => `/runs/${id}/export`,
  createProject: (name: string, visibility: 'public' | 'private') =>
    authSend<CreatedProject>('POST', '/projects', { name, visibility }),
  getProjectAdmin: (name: string) => apiFetch<Project>(`/admin/projects/${encodeURIComponent(name)}`),
  updateProject: (name: string, patch: { visibility?: 'public' | 'private'; members?: string[] }) =>
    authSend<Project>('PATCH', `/admin/projects/${encodeURIComponent(name)}`, patch),

  listUsers: () => apiFetch<UserAccount[]>('/admin/users'),
  createUser: (username: string, password: string, role: 'admin' | 'user') =>
    authSend<{ username: string }>('POST', '/admin/users', { username, password, role }),
  updateUser: (username: string, patch: { password?: string; role?: 'admin' | 'user' }) =>
    authSend<{ updated: string }>('PATCH', `/admin/users/${encodeURIComponent(username)}`, patch),
  deleteUser: (username: string) =>
    authSend<{ deleted: string }>('DELETE', `/admin/users/${encodeURIComponent(username)}`),

  listKeys: () => apiFetch<ApiKey[]>('/admin/keys'),
  createKey: (name: string, project?: string) =>
    authSend<CreatedApiKey>('POST', '/admin/keys', { name, project: project ?? null }),
  listAlerts: () => apiFetch<AlertRule[]>('/admin/alerts'),
  createAlert: (rule: AlertRuleInput) => authSend<AlertRule>('POST', '/admin/alerts', rule),
  updateAlert: (id: string, patch: Partial<AlertRuleInput>) => authSend<AlertRule>('PATCH', `/admin/alerts/${id}`, patch),
  deleteAlert: (id: string) => authSend<{ deleted: string }>('DELETE', `/admin/alerts/${id}`),
  testAlert: (id: string) => authSend<{ sent: boolean }>('POST', `/admin/alerts/${id}/test`),
  revokeKey: (id: string) => authSend<{ revoked: string }>('DELETE', `/admin/keys/${id}`),
  listRuns: async (query: RunQuery = {}): Promise<{ runs: Run[]; meta: RunListMeta | null }> => {
    const { data, meta } = await apiFetchFull<Run[]>(`/runs${qs({ ...query })}`)
    return { runs: data, meta: (meta as RunListMeta | undefined) ?? null }
  },
  listPins: (include_archived = false) => apiFetch<Run[]>(`/pins${qs({ include_archived })}`),
  pinRun: (id: string) => authSend<{ pinned: boolean }>('PUT', `/runs/${id}/pin`),
  unpinRun: (id: string) => authSend<{ pinned: boolean }>('DELETE', `/runs/${id}/pin`),
  updateRun: (id: string, patch: { name?: string; tags?: string[]; notes?: string; group?: string | null }) =>
    authSend<{ updated: boolean }>('PATCH', `/runs/${id}`, patch),
  archiveRun: (id: string, archive: boolean) =>
    authSend<unknown>('POST', `/runs/${id}/${archive ? 'archive' : 'unarchive'}`),
  moveRun: (id: string, project: string, force = false) =>
    authSend<unknown>('POST', `/runs/${id}/move`, { project, force }),
  deleteRun: (id: string) => authSend<unknown>('DELETE', `/runs/${id}`),
  bulkRuns: (body: {
    run_ids: string[]
    action: 'tag' | 'archive' | 'unarchive' | 'move' | 'delete'
    add_tags?: string[]
    remove_tags?: string[]
    project?: string
    force?: boolean
  }) => authSend<BulkResponse>('POST', '/runs/bulk', body),
  getRun: (id: string) => apiFetch<Run>(`/runs/${id}`),
  getMetricNames: (id: string) => apiFetch<string[]>(`/runs/${id}/metric-names`),
  getMetrics: (id: string, name?: string, query: SeriesQuery = {}) =>
    apiFetch<Metric[]>(`/runs/${id}/metrics${qs({ name, ...query })}`),
  getMetricsSeries: async (id: string, name: string | undefined, query: SeriesQuery): Promise<SeriesResult<Metric>> => {
    const { data, meta } = await apiFetchFull<Metric[]>(`/runs/${id}/metrics${qs({ name, ...query })}`)
    return { data, downsampled: !!meta?.downsampled }
  },
  getSystemSnapshots: (id: string, query: SeriesQuery = {}) =>
    apiFetch<SystemSnapshot[]>(`/runs/${id}/system${qs({ ...query })}`),
  getSystemSeries: async (id: string, query: SeriesQuery): Promise<SeriesResult<SystemSnapshot>> => {
    const { data, meta } = await apiFetchFull<SystemSnapshot[]>(`/runs/${id}/system${qs({ ...query })}`)
    return { data, downsampled: !!meta?.downsampled }
  },
  getRunFacets: (opts: { project?: string; include_archived?: boolean } = {}) =>
    apiFetch<RunFacets>(`/runs/facets${qs({ ...opts })}`),
  getProfileSteps: (id: string) => apiFetch<number[]>(`/runs/${id}/profile/steps`),
  getProfileEvents: (id: string, step?: number) =>
    apiFetch<ProfileEvent[]>(
      `/runs/${id}/profile${step !== undefined ? `?step=${step}` : ''}`,
    ),
  getParamStatNames: (id: string) => apiFetch<string[]>(`/runs/${id}/param-stats/names`),
  getParamStats: (id: string, name?: string, query: SeriesQuery = {}) =>
    apiFetch<ParamStat[]>(`/runs/${id}/param-stats${qs({ name, ...query })}`),
  getParamStatsSeries: async (id: string, query: SeriesQuery): Promise<SeriesResult<ParamStat>> => {
    const { data, meta } = await apiFetchFull<ParamStat[]>(`/runs/${id}/param-stats${qs({ ...query })}`)
    return { data, downsampled: !!meta?.downsampled }
  },
  getEvents: (id: string) => apiFetch<RunEvent[]>(`/runs/${id}/events`),
  getLogs: (id: string, stream?: string) =>
    apiFetch<RunLog[]>(`/runs/${id}/logs${stream ? `?stream=${encodeURIComponent(stream)}` : ''}`),
  getArtifacts: (id: string) => apiFetch<Artifact[]>(`/runs/${id}/artifacts`),
  getMediaNames: (id: string) => apiFetch<string[]>(`/runs/${id}/media/names`),
  /** Images are loaded lazily through each item's `url` (`inline=false`: no base64 in the list). */
  getMedia: (id: string, name?: string) =>
    apiFetch<MediaItem[]>(`/runs/${id}/media${qs({ name, inline: 'false' })}`),
  getSampleSteps: (id: string) => apiFetch<SampleStep[]>(`/runs/${id}/samples/steps`),
  getSamples: (
    id: string,
    name: string,
    step: number,
    offset = 0,
    limit = 50,
    query: { sort?: string | null; desc?: boolean; q?: string; prompt?: string | null } = {},
  ) => {
    const p = new URLSearchParams({ name, step: String(step), offset: String(offset), limit: String(limit) })
    if (query.sort) {
      p.set('sort', query.sort)
      p.set('order', query.desc ? 'desc' : 'asc')
    }
    if (query.q) p.set('q', query.q)
    if (query.prompt != null) p.set('prompt', query.prompt)
    return apiFetch<SamplesPage>(`/runs/${id}/samples?${p.toString()}`)
  },
  getRunSummary: (id: string) => apiFetch<RunSummary>(`/runs/${id}/summary`),
  getLlmSummary: (id: string) => fetch(`/runs/${id}/llm-summary`).then((r) => {
    if (!r.ok) throw new Error(r.statusText)
    return r.text()
  }),
}

/** Pins have no WebSocket event; whoever changes one tells the sidebar through this window event. */
export const PINS_CHANGED = 'pyrometer:pins-changed'

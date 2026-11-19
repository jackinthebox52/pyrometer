import type { ApiResponse, Artifact, MediaItem, Metric, ParamStat, ProfileEvent, Run, RunEvent, RunLog, RunSummary, SystemSnapshot } from './types.ts'

async function apiFetch<T>(path: string): Promise<T> {
  const res = await fetch(path)
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
  return json.data
}

export const api = {
  listRuns: () => apiFetch<Run[]>('/runs'),
  getRun: (id: string) => apiFetch<Run>(`/runs/${id}`),
  getMetricNames: (id: string) => apiFetch<string[]>(`/runs/${id}/metric-names`),
  getMetrics: (id: string, name?: string) =>
    apiFetch<Metric[]>(
      `/runs/${id}/metrics${name ? `?name=${encodeURIComponent(name)}` : ''}`,
    ),
  getSystemSnapshots: (id: string) => apiFetch<SystemSnapshot[]>(`/runs/${id}/system`),
  getProfileSteps: (id: string) => apiFetch<number[]>(`/runs/${id}/profile/steps`),
  getProfileEvents: (id: string, step?: number) =>
    apiFetch<ProfileEvent[]>(
      `/runs/${id}/profile${step !== undefined ? `?step=${step}` : ''}`,
    ),
  getParamStatNames: (id: string) => apiFetch<string[]>(`/runs/${id}/param-stats/names`),
  getParamStats: (id: string, name?: string) =>
    apiFetch<ParamStat[]>(
      `/runs/${id}/param-stats${name ? `?name=${encodeURIComponent(name)}` : ''}`,
    ),
  getEvents: (id: string) => apiFetch<RunEvent[]>(`/runs/${id}/events`),
  getLogs: (id: string, stream?: string) =>
    apiFetch<RunLog[]>(`/runs/${id}/logs${stream ? `?stream=${encodeURIComponent(stream)}` : ''}`),
  getArtifacts: (id: string) => apiFetch<Artifact[]>(`/runs/${id}/artifacts`),
  getMediaNames: (id: string) => apiFetch<string[]>(`/runs/${id}/media/names`),
  getMedia: (id: string, name?: string) =>
    apiFetch<MediaItem[]>(
      `/runs/${id}/media${name ? `?name=${encodeURIComponent(name)}` : ''}`,
    ),
  getRunSummary: (id: string) => apiFetch<RunSummary>(`/runs/${id}/summary`),
  getLlmSummary: (id: string) => fetch(`/runs/${id}/llm-summary`).then((r) => {
    if (!r.ok) throw new Error(r.statusText)
    return r.text()
  }),
}

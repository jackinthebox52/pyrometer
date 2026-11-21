import type { Run } from '../api/types.ts'

export type SortKey = 'updated' | 'created' | 'name'

/** Filters applied to the sidebar run list (server-side ones are also re-applied to live updates). */
export interface RunFilters {
  status: string
  project: string
  group: string
  tags: string[]
  /** inclusive `YYYY-MM-DD` bounds on `created_at` (local time), either may be empty */
  from: string
  to: string
  query: string
  includeArchived: boolean
}

export const EMPTY_FILTERS: RunFilters = {
  status: '',
  project: '',
  group: '',
  tags: [],
  from: '',
  to: '',
  query: '',
  includeArchived: false,
}

function ms(iso: string | undefined | null): number {
  const t = iso ? Date.parse(iso) : NaN
  return Number.isNaN(t) ? 0 : t
}

/** Same order as `GET /runs?sort=`: updated/created newest first, name a-z, run_id as the tiebreak. */
export function sortRuns(runs: Run[], sort: SortKey): Run[] {
  const key = (r: Run) => (sort === 'created' ? ms(r.created_at) : ms(r.updated_at ?? r.created_at))
  return [...runs].sort((a, b) => {
    if (sort === 'name') {
      const c = a.name.localeCompare(b.name)
      if (c !== 0) return c
    } else {
      const d = key(b) - key(a)
      if (d !== 0) return d
    }
    return a.run_id < b.run_id ? -1 : a.run_id > b.run_id ? 1 : 0
  })
}

/** Local-midnight Unix seconds of a `YYYY-MM-DD` day (`plusDays` moves the day); undefined when empty/invalid. */
function dayStart(day: string, plusDays = 0): number | undefined {
  if (!day) return undefined
  const d = new Date(`${day}T00:00:00`)
  if (Number.isNaN(d.getTime())) return undefined
  d.setDate(d.getDate() + plusDays)
  return d.getTime() / 1000
}

/**
 * The `GET /runs` parameters for the filters: every filter is evaluated by the server, so the sidebar pages
 * through matches instead of loading all runs. `to` is inclusive, so `created_before` is the next midnight.
 */
export function filtersToQuery(f: RunFilters): {
  include_archived: boolean
  project: string
  group: string
  status: string
  q: string
  tags: string
  created_after?: number
  created_before?: number
} {
  return {
    include_archived: f.includeArchived,
    project: f.project,
    group: f.group,
    status: f.status,
    q: f.query.trim(),
    tags: f.tags.join(','),
    created_after: dayStart(f.from),
    created_before: dayStart(f.to, 1),
  }
}

/** Does a run pass the filters? Used to decide whether a live-updated run belongs in the list. */
export function matchesFilters(run: Run, f: RunFilters): boolean {
  if (!f.includeArchived && (run.archived || run.project_archived)) return false
  if (f.status && run.status !== f.status) return false
  if (f.project && run.project !== f.project) return false
  if (f.group && (run.group ?? '') !== f.group) return false
  if (f.tags.length > 0 && !f.tags.every((t) => run.tags.includes(t))) return false
  const created = ms(run.created_at)
  if (f.from && created < new Date(`${f.from}T00:00:00`).getTime()) return false
  if (f.to && created >= new Date(`${f.to}T00:00:00`).getTime() + 86_400_000) return false
  const q = f.query.trim().toLowerCase()
  if (q) {
    const hay = [run.name, run.group ?? '', ...run.tags].map((s) => s.toLowerCase())
    if (!hay.some((s) => s.includes(q))) return false
  }
  return true
}

/**
 * Merge a freshly fetched page (or a live update) into the list: dedupe by run_id with the incoming
 * copy winning (offset paging is not a snapshot, so a run can show up on two pages), then re-sort.
 */
export function mergeRuns(existing: Run[], incoming: Run[], sort: SortKey): Run[] {
  const byId = new Map<string, Run>()
  for (const r of existing) byId.set(r.run_id, r)
  for (const r of incoming) byId.set(r.run_id, r)
  return sortRuns([...byId.values()], sort)
}

export function removeRun(runs: Run[], runId: string): Run[] {
  return runs.some((r) => r.run_id === runId) ? runs.filter((r) => r.run_id !== runId) : runs
}

/** Upsert a live-updated run, or drop it if it no longer passes the filters. */
export function applyLiveRun(runs: Run[], run: Run, f: RunFilters, sort: SortKey): Run[] {
  return matchesFilters(run, f) ? mergeRuns(runs, [run], sort) : removeRun(runs, run.run_id)
}

export type SidebarItem =
  | { kind: 'run'; run: Run }
  | { kind: 'group'; key: string; group: string; project: string; runs: Run[]; summary: string }

/** "3 running · 2 completed" for a group header, most active states first. */
export function statusSummary(runs: Run[]): string {
  const counts: Record<string, number> = {}
  for (const r of runs) counts[r.status] = (counts[r.status] ?? 0) + 1
  return summarizeStatuses(counts)
}

/** Same text from status counts (the server's facets cover runs that are not loaded yet). */
export function summarizeStatuses(statuses: Record<string, number>): string {
  const order = ['running', 'crashed', 'failed', 'killed', 'completed']
  const counts = new Map(Object.entries(statuses))
  const keys = [...counts.keys()].sort((a, b) => {
    const ia = order.indexOf(a)
    const ib = order.indexOf(b)
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)
  })
  return keys.map((k) => `${counts.get(k)} ${k}`).join(' · ')
}

/**
 * Nest runs that share a `group` (within one project) under one entry. Entries keep the order of
 * their first (best-ranked) member, so a group sits where its newest run would.
 */
export function buildSidebarItems(runs: Run[]): SidebarItem[] {
  const items: SidebarItem[] = []
  const groups = new Map<string, Extract<SidebarItem, { kind: 'group' }>>()
  for (const run of runs) {
    if (!run.group) {
      items.push({ kind: 'run', run })
      continue
    }
    const key = `${run.project}\u0000${run.group}`
    let g = groups.get(key)
    if (!g) {
      g = { kind: 'group', key, group: run.group, project: run.project, runs: [], summary: '' }
      groups.set(key, g)
      items.push(g)
    }
    g.runs.push(run)
  }
  for (const g of groups.values()) g.summary = statusSummary(g.runs)
  return items
}

/** Runs in the order they are drawn (collapsed groups hide their members), for shift-click ranges. */
export function visibleRunIds(items: SidebarItem[], collapsed: Set<string>): string[] {
  const ids: string[] = []
  for (const it of items) {
    if (it.kind === 'run') ids.push(it.run.run_id)
    else if (!collapsed.has(it.key)) ids.push(...it.runs.map((r) => r.run_id))
  }
  return ids
}

/** Inclusive slice of `order` between two ids (either direction); empty if either is missing. */
export function rangeBetween(order: string[], a: string, b: string): string[] {
  const i = order.indexOf(a)
  const j = order.indexOf(b)
  if (i < 0 || j < 0) return []
  return order.slice(Math.min(i, j), Math.max(i, j) + 1)
}

/** Tags used across the loaded runs, most common first (fallback when the server's facets are unavailable). */
export function collectTags(runs: Run[], limit = 12): string[] {
  const counts = new Map<string, number>()
  for (const r of runs) for (const t of r.tags) counts.set(t, (counts.get(t) ?? 0) + 1)
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, limit).map(([t]) => t)
}

/** Parse "a, b ,c" into trimmed, deduplicated, non-empty tags. */
export function parseTags(text: string): string[] {
  return [...new Set(text.split(',').map((t) => t.trim().replace(/^#/, '')).filter(Boolean))]
}

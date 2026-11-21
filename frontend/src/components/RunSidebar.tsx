import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { NavLink, useMatch, useNavigate } from 'react-router-dom'
import { api, PINS_CHANGED, type RunSort } from '../api/client.ts'
import type { BulkResponse, Project, Run, RunFacets } from '../api/types.ts'
import { timeAgo } from '../utils/format.ts'
import { RUN_COLORS } from '../pages/CompareView.tsx'
import { useWebSocket } from '../hooks/useWebSocket.ts'
import { useSession } from '../hooks/useSession.ts'
import ConfirmDialog from './ConfirmDialog.tsx'
import {
  EMPTY_FILTERS,
  applyLiveRun,
  buildSidebarItems,
  collectTags,
  filtersToQuery,
  matchesFilters,
  mergeRuns,
  parseTags,
  rangeBetween,
  removeRun,
  summarizeStatuses,
  visibleRunIds,
  type RunFilters,
  type SidebarItem,
} from '../utils/runList.ts'

const STATUS_COLOR: Record<string, string> = {
  running: '#22c55e',
  completed: '#60a5fa',
  failed: '#f97316',
  crashed: '#ef4444',
  killed: '#eab308',
}

const PAGE_SIZE = 50
const ACTIVITY_REFRESH_MS = 2000 // run_activity events refresh the first page at most this often
const FACETS_REFRESH_MS = 3000
const SEARCH_DEBOUNCE_MS = 300
const field =
  'w-full rounded border border-py-border bg-py-base px-2 py-1 text-xs text-py-text outline-none focus:border-py-border-hi'
const smallBtn = 'rounded border border-py-border px-2 py-1 text-xs font-mono text-py-muted disabled:opacity-40'

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : fallback
  } catch {
    return fallback
  }
}
function save(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // storage unavailable: preferences just won't persist
  }
}

interface Props {
  selectedIds: string[]
  onToggleSelect: (id: string) => void
  /** Select (on=true) or deselect many runs at once (shift-click ranges, groups, bulk results). */
  onSelectMany: (ids: string[], on: boolean) => void
  onClearSelection: () => void
}

export default function RunSidebar({ selectedIds, onToggleSelect, onSelectMany, onClearSelection }: Props) {
  const { isAdmin } = useSession()
  const [runs, setRuns] = useState<Run[]>([])
  const [pins, setPins] = useState<Run[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [facets, setFacets] = useState<RunFacets | null>(null)
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [serverDown, setServerDown] = useState(false)
  const [sort, setSort] = useState<RunSort>(() => load<RunSort>('pyro.sidebar.sort', 'updated'))
  const [filters, setFilters] = useState<RunFilters>(EMPTY_FILTERS)
  const [showFilters, setShowFilters] = useState(false)
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set(load<string[]>('pyro.sidebar.collapsed', [])))
  const [bulkOpen, setBulkOpen] = useState(false)
  const navigate = useNavigate()

  const offsetRef = useRef(0) // server rows consumed so far (not runs.length: dedupe and live inserts skew that)
  const genRef = useRef(0) // bumped on every reset so late responses from an old query are dropped
  const loadingRef = useRef(false)
  const lastClicked = useRef<string | null>(null)
  // The server evaluates every filter; the search text is debounced so typing does not fire a query per key.
  const serverFilters = useMemo(() => ({ ...filters, query: debouncedQuery }), [filters, debouncedQuery])
  const stateRef = useRef({ filters, serverFilters, sort, pins, selectedIds })
  stateRef.current = { filters, serverFilters, sort, pins, selectedIds }

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(filters.query), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [filters.query])

  const serverKey = JSON.stringify([sort, filtersToQuery(serverFilters)])

  const fetchPage = useCallback(async (reset: boolean) => {
    const { serverFilters: f, sort: s } = stateRef.current
    if (!reset && loadingRef.current) return
    const gen = reset ? ++genRef.current : genRef.current
    const offset = reset ? 0 : offsetRef.current
    loadingRef.current = true
    setLoading(true)
    try {
      const { runs: page, meta } = await api.listRuns({
        sort: s,
        limit: PAGE_SIZE,
        offset,
        ...filtersToQuery(f),
      })
      if (gen !== genRef.current) return
      offsetRef.current = offset + page.length
      setTotal(meta?.total ?? offset + page.length)
      setRuns((prev) => mergeRuns(reset ? [] : prev, page, s))
      setServerDown(false)
    } catch {
      if (gen === genRef.current) setServerDown(true)
    } finally {
      if (gen === genRef.current) {
        loadingRef.current = false
        setLoading(false)
      }
    }
  }, [])

  const refreshPins = useCallback(async () => {
    try {
      setPins(await api.listPins(stateRef.current.filters.includeArchived))
    } catch {
      setPins([]) // API keys and similar principals have no pins
    }
  }, [])

  const refreshProjects = useCallback(async () => {
    try {
      setProjects(await api.listProjects({ include_archived: stateRef.current.filters.includeArchived }))
    } catch {
      // the selector just stays as it was
    }
  }, [])

  // (Re)load from the first page whenever a server-side query parameter changes.
  useEffect(() => {
    setRuns([])
    fetchPage(true)
    refreshPins()
    refreshProjects()
  }, [serverKey, fetchPage, refreshPins, refreshProjects])

  const facetsTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const refreshFacets = useCallback(async () => {
    const f = stateRef.current.filters
    try {
      setFacets(await api.getRunFacets({ project: f.project, include_archived: f.includeArchived }))
    } catch {
      // keep the previous options
    }
  }, [])
  /** Trailing-throttled: bursts of run events cost one facets query. */
  const scheduleFacets = useCallback(() => {
    if (facetsTimer.current) return
    facetsTimer.current = setTimeout(() => {
      facetsTimer.current = null
      refreshFacets()
    }, FACETS_REFRESH_MS)
  }, [refreshFacets])
  useEffect(() => {
    refreshFacets()
  }, [filters.project, filters.includeArchived, refreshFacets])

  // Live runs: the server pushes a throttled run_activity event when a run ingests data. Re-read the first
  // page (cheap, one query) at most every ACTIVITY_REFRESH_MS instead of polling.
  const activityTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const scheduleActivityRefresh = useCallback(() => {
    if (activityTimer.current) return
    activityTimer.current = setTimeout(async () => {
      activityTimer.current = null
      const { serverFilters: f, sort: s } = stateRef.current
      try {
        const { runs: page } = await api.listRuns({ sort: s, limit: PAGE_SIZE, ...filtersToQuery(f) })
        setRuns((prev) => mergeRuns(prev, page, s))
        setServerDown(false)
        if (stateRef.current.pins.length > 0) refreshPins() // pinned runs may have changed status
      } catch {
        setServerDown(true)
      }
    }, ACTIVITY_REFRESH_MS)
  }, [refreshPins])
  useEffect(() => () => {
    if (activityTimer.current) clearTimeout(activityTimer.current)
    if (facetsTimer.current) clearTimeout(facetsTimer.current)
  }, [])

  useEffect(() => {
    const h = () => refreshPins()
    window.addEventListener(PINS_CHANGED, h)
    return () => window.removeEventListener(PINS_CHANGED, h)
  }, [refreshPins])

  const drop = useCallback((runId: string) => {
    setRuns((prev) => removeRun(prev, runId))
    setPins((prev) => removeRun(prev, runId))
    if (stateRef.current.selectedIds.includes(runId)) onSelectMany([runId], false)
  }, [onSelectMany])

  const upsert = useCallback(async (runId: string) => {
    try {
      const run = await api.getRun(runId)
      const { serverFilters: f, sort: s, pins: p } = stateRef.current
      setRuns((prev) => applyLiveRun(prev, run, f, s))
      const isPinned = p.some((x) => x.run_id === runId)
      if (isPinned && !matchesFilters(run, f)) setPins((prev) => removeRun(prev, runId))
      else if (isPinned) setPins((prev) => prev.map((x) => (x.run_id === runId ? run : x)))
      if (!!run.pinned !== isPinned) refreshPins()
    } catch {
      drop(runId) // gone or no longer visible to us
    }
  }, [drop, refreshPins])

  useWebSocket((msg) => {
    switch (msg.type) {
      case 'run_activity':
        scheduleActivityRefresh()
        break
      case 'run_created':
      case 'run_updated':
      case 'run_moved':
        upsert(msg.run_id)
        scheduleFacets()
        break
      case 'run_deleted':
        drop(msg.run_id)
        scheduleFacets()
        break
      case 'project_updated':
        fetchPage(true)
        refreshPins()
        refreshProjects()
        refreshFacets()
        break
      case 'project_deleted': {
        const gone = (msg as { project?: string }).project
        if (gone && stateRef.current.filters.project === gone) setFilters((f) => ({ ...f, project: '' }))
        else fetchPage(true)
        refreshPins()
        refreshProjects()
        refreshFacets()
        break
      }
    }
  })

  const pinnedIds = useMemo(() => new Set(pins.map((p) => p.run_id)), [pins])
  const visible = useMemo(() => runs.filter((r) => matchesFilters(r, filters) && !pinnedIds.has(r.run_id)), [runs, filters, pinnedIds])
  const visiblePins = useMemo(() => pins.filter((r) => matchesFilters(r, filters)), [pins, filters])
  const items = useMemo(() => buildSidebarItems(visible), [visible])
  const order = useMemo(
    () => [...visiblePins.map((r) => r.run_id), ...visibleRunIds(items, collapsed)],
    [visiblePins, items, collapsed],
  )
  // Filter options come from the server (all visible runs, not just the loaded pages); loaded runs are the fallback.
  const tagChoices = useMemo(
    () => [...new Set([...filters.tags, ...(facets ? facets.tags.slice(0, 20).map((t) => t.tag) : collectTags([...runs, ...pins]))])],
    [facets, runs, pins, filters.tags],
  )
  const groupChoices = useMemo(
    () => [...new Set([
      ...(filters.group ? [filters.group] : []),
      ...(facets ? facets.groups.map((g) => g.group) : runs.map((r) => r.group).filter((g): g is string => !!g)),
    ])].sort(),
    [facets, runs, filters.group],
  )
  const groupFacets = useMemo(
    () => new Map((facets?.groups ?? []).map((g) => [`${g.project}\u0000${g.group}`, g])),
    [facets],
  )
  const selectedSet = new Set(selectedIds)
  const isComparing = selectedIds.length >= 2
  const hasMore = offsetRef.current < total
  const activeFilters = filters.status || filters.project || filters.group || filters.tags.length || filters.from || filters.to || filters.includeArchived

  function setFilter(patch: Partial<RunFilters>) {
    setFilters((f) => ({ ...f, ...patch }))
  }

  function toggleCollapsed(key: string) {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (!next.delete(key)) next.add(key)
      save('pyro.sidebar.collapsed', [...next])
      return next
    })
  }

  function clickSelect(id: string, shift: boolean) {
    if (shift && lastClicked.current && lastClicked.current !== id) {
      onSelectMany(rangeBetween(order, lastClicked.current, id), true)
    } else {
      onToggleSelect(id)
    }
    lastClicked.current = id
  }

  function onScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 120 && hasMore && !loading) fetchPage(false)
  }

  function renderRun(run: Run, nested: boolean) {
    const isSelected = selectedSet.has(run.run_id)
    const idx = selectedIds.indexOf(run.run_id)
    const color = idx >= 0 ? RUN_COLORS[idx % RUN_COLORS.length] : undefined
    return (
      <div
        key={run.run_id}
        className="flex items-stretch mx-1 my-px rounded overflow-hidden"
        style={{ backgroundColor: isSelected ? 'rgba(245,158,11,0.05)' : undefined, marginLeft: nested ? 14 : undefined, opacity: run.archived ? 0.6 : 1 }}
      >
        <button
          role="checkbox"
          aria-checked={isSelected}
          aria-label={`Select ${run.name}`}
          onClick={(e) => clickSelect(run.run_id, e.shiftKey)}
          title="Select (shift-click for a range)"
          className="flex items-center justify-center flex-shrink-0 px-2 rounded-l"
          style={{ width: 28, background: 'transparent', border: 'none', cursor: 'pointer' }}
        >
          <span
            style={{
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center', width: 14, height: 14, borderRadius: 3,
              border: `1.5px solid ${isSelected ? (color ?? '#f59e0b') : '#2d3140'}`,
              backgroundColor: isSelected ? `${color ?? '#f59e0b'}22` : 'transparent',
              fontSize: 9, color: color ?? '#f59e0b',
            }}
          >
            {isSelected && '✓'}
          </span>
        </button>
        <NavLink
          to={`/runs/${run.run_id}`}
          className={({ isActive }) => `run-nav-link flex-1${isActive ? ' active' : ''}`}
          style={{ margin: 0, borderRadius: 0, minWidth: 0 }}
        >
          <div className="flex items-center gap-2 min-w-0">
            <span
              className={run.status === 'running' ? 'status-pulse' : ''}
              style={{ display: 'inline-block', width: 6, height: 6, borderRadius: '50%', backgroundColor: STATUS_COLOR[run.status] ?? '#4a4e60', flexShrink: 0 }}
            />
            <span className="truncate text-sm font-medium leading-snug" style={{ color: '#e8eaf0' }}>{run.name}</span>
            {run.pinned && <span aria-label="pinned" title="Pinned" style={{ color: '#f59e0b', fontSize: 10 }}>★</span>}
          </div>
          <div className="flex items-center gap-2 mt-0.5 pl-[14px]">
            <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>
              {timeAgo(sort === 'created' ? run.created_at : (run.updated_at ?? run.created_at))}
            </span>
            {run.archived && <span className="text-xs font-mono text-py-killed">archived</span>}
            {run.tags.slice(0, 2).map((tag) => (
              <span key={tag} className="text-xs truncate" style={{ color: '#4a4e60' }}>#{tag}</span>
            ))}
          </div>
        </NavLink>
      </div>
    )
  }

  function renderItem(it: SidebarItem) {
    if (it.kind === 'run') return renderRun(it.run, false)
    const open = !collapsed.has(it.key)
    const ids = it.runs.map((r) => r.run_id)
    const gf = groupFacets.get(it.key)
    const allSel = ids.every((id) => selectedSet.has(id))
    return (
      <div key={it.key}>
        <div className="flex items-center mx-1 my-px rounded" style={{ backgroundColor: '#15171d' }}>
          <button
            role="checkbox"
            aria-checked={allSel}
            aria-label={`Select all runs in ${it.group}`}
            onClick={() => onSelectMany(ids, !allSel)}
            className="flex items-center justify-center px-2"
            style={{ width: 28, background: 'transparent', border: 'none', cursor: 'pointer', color: '#f59e0b', fontSize: 9 }}
          >
            <span style={{ width: 14, height: 14, borderRadius: 3, border: `1.5px solid ${allSel ? '#f59e0b' : '#2d3140'}`, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}>
              {allSel && '✓'}
            </span>
          </button>
          <button
            onClick={() => toggleCollapsed(it.key)}
            aria-expanded={open}
            title={`${it.group} (${it.project})`}
            className="flex-1 min-w-0 text-left py-1.5 pr-2"
            style={{ background: 'transparent', border: 'none', cursor: 'pointer' }}
          >
            <div className="flex items-center gap-1.5 min-w-0">
              <span style={{ color: '#8b8fa8', fontSize: 9 }}>{open ? '▾' : '▸'}</span>
              <span className="truncate text-xs font-semibold" style={{ color: '#e8eaf0' }}>{it.group}</span>
              <span className="text-xs font-mono" style={{ color: '#4a4e60' }} title={gf && gf.runs > it.runs.length ? `${it.runs.length} of ${gf.runs} runs loaded` : undefined}>
                {gf && gf.runs > it.runs.length ? `${it.runs.length}/${gf.runs}` : it.runs.length}
              </span>
            </div>
            <div className="pl-[15px] text-xs font-mono truncate" style={{ color: '#4a4e60' }}>{gf ? summarizeStatuses(gf.statuses) : it.summary}</div>
          </button>
        </div>
        {open && it.runs.map((r) => renderRun(r, true))}
      </div>
    )
  }

  const sectionLabel = 'px-4 pt-2 pb-1 text-[10px] font-mono uppercase tracking-widest text-py-faint'

  return (
    <aside className="flex flex-col border-r" style={{ width: 288, minWidth: 288, backgroundColor: '#111318', borderColor: '#252830' }}>
      <div className="flex items-center gap-2.5 px-4 py-[14px] border-b" style={{ borderColor: '#252830' }}>
        <span style={{ color: '#f59e0b', fontSize: 17, lineHeight: 1 }}>◉</span>
        <span className="font-semibold tracking-widest text-xs uppercase" style={{ color: '#e8eaf0', letterSpacing: '0.18em' }}>Pyrometer</span>
      </div>

      {/* Search + sort + filter toggle */}
      <div className="px-3 py-2.5 border-b flex flex-col gap-2" style={{ borderColor: '#252830' }}>
        <input
          type="search"
          aria-label="Filter runs by name, group or tag"
          placeholder="Filter by name, group or tag…"
          value={filters.query}
          onChange={(e) => setFilter({ query: e.target.value })}
          className={`${field} px-3 py-1.5`}
        />
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-py-faint">
            Sort
            <select
              aria-label="Sort runs"
              value={sort}
              onChange={(e) => {
                const s = e.target.value as RunSort
                setSort(s)
                save('pyro.sidebar.sort', s)
              }}
              className={field}
            >
              <option value="updated">Last updated</option>
              <option value="created">Created</option>
              <option value="name">Name</option>
            </select>
          </label>
          <button
            onClick={() => setShowFilters((v) => !v)}
            aria-expanded={showFilters}
            className={smallBtn}
            style={activeFilters ? { color: '#f59e0b', borderColor: '#f59e0b66' } : undefined}
          >
            Filters{activeFilters ? ' •' : ''}
          </button>
        </div>
        {showFilters && (
          <div className="flex flex-col gap-2" role="group" aria-label="Run filters">
            <select aria-label="Project" value={filters.project} onChange={(e) => setFilter({ project: e.target.value })} className={field}>
              <option value="">All projects</option>
              {projects.map((p) => (
                <option key={p.name} value={p.name}>{p.name}{p.archived ? ' (archived)' : ''}</option>
              ))}
            </select>
            <select aria-label="Status" value={filters.status} onChange={(e) => setFilter({ status: e.target.value })} className={field}>
              <option value="">Any status</option>
              {Object.keys(STATUS_COLOR).map((s) => <option key={s} value={s}>{s}{facets?.statuses[s] ? ` (${facets.statuses[s]})` : ''}</option>)}
            </select>
            <select aria-label="Group" value={filters.group} onChange={(e) => setFilter({ group: e.target.value })} className={field}>
              <option value="">Any group</option>
              {groupChoices.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
            <div className="flex items-center gap-1">
              <input type="date" aria-label="Created from" value={filters.from} onChange={(e) => setFilter({ from: e.target.value })} className={field} />
              <span className="text-py-faint text-xs">–</span>
              <input type="date" aria-label="Created to" value={filters.to} onChange={(e) => setFilter({ to: e.target.value })} className={field} />
            </div>
            {tagChoices.length > 0 && (
              <div className="flex flex-wrap gap-1" role="group" aria-label="Filter by tag">
                {tagChoices.map((t) => {
                  const on = filters.tags.includes(t)
                  return (
                    <button
                      key={t}
                      aria-pressed={on}
                      onClick={() => setFilter({ tags: on ? filters.tags.filter((x) => x !== t) : [...filters.tags, t] })}
                      className="rounded px-1.5 py-0.5 text-xs font-mono"
                      style={{ border: `1px solid ${on ? '#f59e0b' : '#252830'}`, color: on ? '#f59e0b' : '#8b8fa8', background: 'transparent', cursor: 'pointer' }}
                    >
                      #{t}
                    </button>
                  )
                })}
              </div>
            )}
            <label className="flex items-center gap-2 text-xs text-py-muted">
              <input type="checkbox" checked={filters.includeArchived} onChange={(e) => setFilter({ includeArchived: e.target.checked })} />
              Show archived
            </label>
            {activeFilters && (
              <button className={smallBtn} onClick={() => setFilters({ ...EMPTY_FILTERS, query: filters.query })}>Clear filters</button>
            )}
          </div>
        )}
      </div>

      {/* Run list */}
      <div className="flex-1 overflow-y-auto py-1" onScroll={onScroll}>
        {serverDown && <div className="px-4 py-3 text-xs font-mono" style={{ color: '#4a4e60' }}>⚠ Server unreachable</div>}
        {!serverDown && !loading && visible.length === 0 && visiblePins.length === 0 && (
          <div className="px-4 py-4 text-xs" style={{ color: '#4a4e60' }}>
            {filters.query || activeFilters ? 'No matching runs' : 'No runs yet'}
          </div>
        )}
        {visiblePins.length > 0 && (
          <section aria-label="Pinned runs">
            <div className={sectionLabel}>Pinned</div>
            {visiblePins.map((r) => renderRun(r, false))}
            <div className={sectionLabel}>Runs</div>
          </section>
        )}
        {items.map(renderItem)}
        {hasMore && !serverDown && (
          <div className="px-3 py-2">
            <button className={`${smallBtn} w-full`} disabled={loading} onClick={() => fetchPage(false)}>
              {loading ? 'Loading…' : `Load more (${Math.max(total - offsetRef.current, 0)} left)`}
            </button>
          </div>
        )}
      </div>

      {/* Footer: selection / bulk bar */}
      <div className="border-t" style={{ borderColor: '#252830' }}>
        {selectedIds.length > 0 ? (
          <>
            {isAdmin && bulkOpen && (
              <BulkPanel
                runs={[...runs, ...pins]}
                selectedIds={selectedIds}
                projects={projects}
                onSelectMany={onSelectMany}
                onDone={() => {
                  fetchPage(true)
                  refreshPins()
                }}
              />
            )}
            <div className="flex">
              <button
                onClick={() => navigate(`/compare?ids=${selectedIds.join(',')}`)}
                disabled={!isComparing}
                className="flex-1 px-4 py-2.5 text-xs font-mono text-left disabled:opacity-50"
                style={{ backgroundColor: 'rgba(245, 158, 11, 0.1)', color: '#f59e0b', border: 'none', cursor: isComparing ? 'pointer' : 'default' }}
              >
                {isComparing ? `Compare ${selectedIds.length} runs →` : '1 selected'}
              </button>
              {isAdmin && (
                <button
                  onClick={() => setBulkOpen((v) => !v)}
                  aria-expanded={bulkOpen}
                  className="px-3 py-2.5 text-xs font-mono"
                  style={{ background: 'transparent', color: '#8b8fa8', border: 'none', borderLeft: '1px solid #252830', cursor: 'pointer' }}
                >
                  Bulk
                </button>
              )}
              <button
                onClick={onClearSelection}
                title="Clear selection"
                aria-label="Clear selection"
                className="px-3 py-2.5 text-xs font-mono"
                style={{ background: 'transparent', color: '#4a4e60', border: 'none', borderLeft: '1px solid #252830', cursor: 'pointer' }}
              >
                ✕
              </button>
            </div>
          </>
        ) : (
          <div className="px-4 py-2 text-xs font-mono" style={{ color: '#4a4e60' }}>
            {total} run{total !== 1 ? 's' : ''}{runs.length < total && ` · ${runs.length} loaded`}
          </div>
        )}
      </div>

      <div className="flex items-center justify-between px-4 py-2 border-t text-xs font-mono" style={{ borderColor: '#252830', color: '#4a4e60' }}>
        <NavLink to="/settings" style={{ color: '#8b8fa8' }}>Settings</NavLink>
        <button onClick={() => api.logout().finally(() => window.location.reload())} style={{ background: 'none', border: 'none', color: '#8b8fa8', cursor: 'pointer' }}>
          Sign out
        </button>
      </div>
    </aside>
  )
}

type BulkAction = 'archive' | 'unarchive' | 'move' | 'delete'

/** Admin bulk actions on the selection; shows what failed per run instead of hiding it. */
function BulkPanel({
  runs, selectedIds, projects, onSelectMany, onDone,
}: {
  runs: Run[]
  selectedIds: string[]
  projects: Project[]
  onSelectMany: (ids: string[], on: boolean) => void
  onDone: () => void
}) {
  const [add, setAdd] = useState('')
  const [remove, setRemove] = useState('')
  const [target, setTarget] = useState('')
  const [force, setForce] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [failures, setFailures] = useState<{ name: string; error: string }[]>([])
  const [info, setInfo] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const names = useMemo(() => new Map(runs.map((r) => [r.run_id, r.name])), [runs])
  const match = useMatch('/runs/:runId')

  async function run(body: Parameters<typeof api.bulkRuns>[0], label: string) {
    setBusy(true)
    setError(null)
    setInfo(null)
    try {
      const res: BulkResponse = await api.bulkRuns(body)
      const ok = res.results.filter((r) => r.ok).map((r) => r.run_id)
      setFailures(res.results.filter((r) => !r.ok).map((r) => ({ name: names.get(r.run_id) ?? r.run_id, error: r.error ?? 'failed' })))
      setInfo(`${label}: ${res.succeeded} done${res.failed ? `, ${res.failed} failed` : ''}.`)
      if (body.action === 'delete' || body.action === 'archive') onSelectMany(ok, false)
      onDone()
      if (body.action === 'delete' && match?.params.runId && ok.includes(match.params.runId)) window.location.hash = '#/'
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Bulk action failed')
    } finally {
      setBusy(false)
    }
  }

  const n = selectedIds.length
  const act = (action: BulkAction, label: string) => run({ run_ids: selectedIds, action }, label)

  return (
    <div className="flex flex-col gap-2 border-b p-3" style={{ borderColor: '#252830' }} role="group" aria-label="Bulk actions">
      <div className="flex gap-1">
        <input aria-label="Tags to add" placeholder="add tags: a, b" value={add} onChange={(e) => setAdd(e.target.value)} className={field} />
        <input aria-label="Tags to remove" placeholder="remove tags" value={remove} onChange={(e) => setRemove(e.target.value)} className={field} />
        <button
          className={smallBtn}
          disabled={busy || (!parseTags(add).length && !parseTags(remove).length)}
          onClick={() => run({ run_ids: selectedIds, action: 'tag', add_tags: parseTags(add), remove_tags: parseTags(remove) }, 'Tag').then(() => { setAdd(''); setRemove('') })}
        >
          Tag
        </button>
      </div>
      <div className="flex gap-1">
        <button className={smallBtn} disabled={busy} onClick={() => act('archive', 'Archive')}>Archive</button>
        <button className={smallBtn} disabled={busy} onClick={() => act('unarchive', 'Unarchive')}>Unarchive</button>
        <button className={`${smallBtn} ml-auto`} style={{ color: '#ef4444' }} disabled={busy} onClick={() => setConfirmDelete(true)}>Delete…</button>
      </div>
      <div className="flex items-center gap-1">
        <select aria-label="Move to project" value={target} onChange={(e) => setTarget(e.target.value)} className={field}>
          <option value="">Move to…</option>
          {projects.filter((p) => !p.archived).map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
        </select>
        <label className="flex items-center gap-1 text-xs text-py-faint whitespace-nowrap" title="Needed for runs that are still running">
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> force
        </label>
        <button className={smallBtn} disabled={busy || !target} onClick={() => run({ run_ids: selectedIds, action: 'move', project: target, force }, 'Move')}>Move</button>
      </div>
      {error && <p role="alert" className="text-xs text-py-crashed">{error}</p>}
      {info && <p role="status" className="text-xs text-py-muted">{info}</p>}
      {failures.length > 0 && (
        <ul className="text-xs text-py-crashed max-h-24 overflow-y-auto">
          {failures.map((f, i) => <li key={i}>{f.name}: {f.error}</li>)}
        </ul>
      )}
      {confirmDelete && (
        <ConfirmDialog
          title={`Delete ${n} run${n === 1 ? '' : 's'}?`}
          confirmLabel="Delete permanently"
          requireText="delete"
          danger
          onCancel={() => setConfirmDelete(false)}
          onConfirm={async () => {
            await run({ run_ids: selectedIds, action: 'delete' }, 'Delete')
            setConfirmDelete(false)
          }}
        >
          <p>This removes the selected runs and all their metrics, logs, media and profiles. It cannot be undone.</p>
        </ConfirmDialog>
      )}
    </div>
  )
}

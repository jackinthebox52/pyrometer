import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { NavLink, useMatch, useNavigate } from 'react-router-dom'
import { api, PINS_CHANGED, type RunSort } from '../api/client.ts'
import type { BulkResponse, Project, Run, RunFacets } from '../api/types.ts'
import { timeAgo } from '../utils/format.ts'
import { RUN_COLORS } from '../utils/runColors.ts'
import { useWebSocket } from '../hooks/useWebSocket.ts'
import { useSession } from '../hooks/useSession.ts'
import ConfirmDialog from './ConfirmDialog.tsx'
import Icon from './Icon.tsx'
import { heatColor, isLight } from '../theme.ts'
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

const STATUSES = ['running', 'completed', 'failed', 'crashed', 'killed']

/** Chip colour class for a finished run; running runs are coloured by their GPU heat instead. */
const CHIP_CLASS: Record<string, string> = {
  completed: 'bg-py-steel',
  failed: 'bg-py-red',
  crashed: 'bg-py-red',
  killed: 'bg-py-muted',
}

const PAGE_SIZE = 50
const ACTIVITY_REFRESH_MS = 2000 // run_activity events refresh the first page at most this often
const FACETS_REFRESH_MS = 3000
const SEARCH_DEBOUNCE_MS = 300
const field =
  'h-[30px] w-full rounded-[2px] border border-py-border-hi bg-py-elevated px-2 text-[13px] text-py-text outline-none placeholder:text-py-faint'
const smallBtn =
  'h-7 rounded-[2px] border border-py-border-hi bg-py-surface px-2 text-xs text-py-soft transition-colors duration-150 hover:bg-py-elevated hover:text-py-text disabled:cursor-not-allowed disabled:opacity-50'
function toggleTheme() {
  const next = isLight() ? 'dark' : 'light'
  document.documentElement.dataset.theme = next
  try { localStorage.setItem('pyrometer.theme', next) } catch { /* ignore */ }
  window.location.reload() // chart colours computed in JS pick up the new ramp
}

const quietLink =
  'flex items-center gap-1.5 text-xs text-py-muted transition-colors duration-150 hover:text-py-text'

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
  const activeMatch = useMatch('/runs/:runId')
  const activeRunId = activeMatch?.params.runId

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
        const { runs: page, meta } = await api.listRuns({ sort: s, limit: PAGE_SIZE, ...filtersToQuery(f) })
        if (meta) setTotal(meta.total) // keeps the footer count right as runs appear and disappear
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
        scheduleActivityRefresh() // also refreshes the total
        break
      case 'run_deleted':
        drop(msg.run_id)
        scheduleFacets()
        scheduleActivityRefresh()
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
  const activeFilterCount = [filters.status, filters.group, filters.tags.length, filters.from, filters.to, filters.includeArchived].filter(Boolean).length
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
    const isActive = run.run_id === activeRunId
    const idx = selectedIds.indexOf(run.run_id)
    const color = idx >= 0 ? RUN_COLORS[idx % RUN_COLORS.length] : undefined
    const running = run.status === 'running'
    const stale = !!run.archived || !!run.project_archived
    const chipClass = stale ? 'bg-py-steel opacity-40' : running ? 'status-pulse' : (CHIP_CLASS[run.status] ?? 'bg-py-muted')
    return (
      <div
        key={run.run_id}
        className={`mx-1 my-px flex min-h-[44px] items-stretch rounded-[2px] border transition-colors duration-150 ${
          isActive ? 'border-py-border-hi bg-py-elevated' : 'border-transparent hover:bg-py-surface'
        }${nested ? ' ml-4' : ''}`}
      >
        <button
          role="checkbox"
          aria-checked={isSelected}
          aria-label={`Select ${run.name}`}
          onClick={(e) => clickSelect(run.run_id, e.shiftKey)}
          title="Select (shift-click for a range)"
          className="flex w-7 flex-shrink-0 items-center justify-center"
        >
          <span
            className={`inline-flex h-3.5 w-3.5 items-center justify-center rounded-[2px] border text-py-n0 ${
              isSelected ? 'border-py-soft bg-py-soft' : 'border-py-border-hi'
            }`}
            style={isSelected && color ? { backgroundColor: color, borderColor: color } : undefined}
          >
            {isSelected && <Icon name="check" size={10} />}
          </span>
        </button>
        <NavLink to={`/runs/${run.run_id}`} className="flex min-w-0 flex-1 items-center gap-2 py-1.5 pr-2">
          <span
            aria-hidden="true"
            className={`h-3.5 w-2 flex-shrink-0 rounded-[1px] ${chipClass}`}
            style={running && !stale ? { backgroundColor: heatColor(run.gpu_heat ?? 0.5) } : undefined}
          />
          <span className="flex min-w-0 flex-1 flex-col">
            <span className="flex min-w-0 items-center gap-1.5">
              <span className={`truncate text-[13px] leading-snug ${running ? 'text-py-text' : 'text-py-soft'}`}>{run.name}</span>
              {run.pinned && <Icon name="pin" size={11} className="flex-shrink-0 text-py-muted" />}
            </span>
            <span className="flex min-w-0 items-center gap-1.5 text-[11px] leading-snug text-py-muted">
              <span className="num shrink-0 whitespace-nowrap">
                {timeAgo(sort === 'created' ? run.created_at : (run.updated_at ?? run.created_at))}
              </span>
              {run.archived && <span className="shrink-0">archived</span>}
              {run.tags.slice(0, 2).map((tag) => (
                <span key={tag} className="min-w-0 truncate">#{tag}</span>
              ))}
            </span>
          </span>
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
        <div className="mx-1 mt-1.5 flex items-center rounded-[2px]">
          <button
            role="checkbox"
            aria-checked={allSel}
            aria-label={`Select all runs in ${it.group}`}
            onClick={() => onSelectMany(ids, !allSel)}
            className="flex w-7 items-center justify-center"
          >
            <span className={`inline-flex h-3.5 w-3.5 items-center justify-center rounded-[2px] border text-py-n0 ${allSel ? 'border-py-soft bg-py-soft' : 'border-py-border-hi'}`}>
              {allSel && <Icon name="check" size={10} />}
            </span>
          </button>
          <button
            onClick={() => toggleCollapsed(it.key)}
            aria-expanded={open}
            title={`${it.group} (${it.project})`}
            className="min-w-0 flex-1 py-1.5 pr-2 text-left"
          >
            <span className="flex min-w-0 items-center gap-1.5">
              <Icon name={open ? 'chevron-down' : 'chevron-right'} size={12} className="flex-shrink-0 text-py-muted" />
              <span className="log-label truncate">{it.group}</span>
              <span className="num text-[11px] text-py-muted" title={gf && gf.runs > it.runs.length ? `${it.runs.length} of ${gf.runs} runs loaded` : undefined}>
                {gf && gf.runs > it.runs.length ? `${it.runs.length}/${gf.runs}` : it.runs.length}
              </span>
            </span>
            <span className="num block truncate pl-[18px] text-[11px] text-py-faint">{gf ? summarizeStatuses(gf.statuses) : it.summary}</span>
          </button>
        </div>
        {open && it.runs.map((r) => renderRun(r, true))}
      </div>
    )
  }

  const sectionLabel = 'log-label px-4 pt-3 pb-1'

  return (
    <aside className="flex w-72 min-w-[18rem] flex-col border-r border-py-border bg-py-n0">
      <div className="flex flex-col gap-2 border-b border-py-border px-3 pb-3 pt-4">
        <span className="px-1 text-[15px] font-semibold text-py-text">Pyrometer</span>
        <select aria-label="Project" value={filters.project} onChange={(e) => setFilter({ project: e.target.value })} className={field}>
          <option value="">All projects</option>
          {projects.map((p) => (
            <option key={p.name} value={p.name}>{p.name}{p.archived ? ' (archived)' : ''}</option>
          ))}
        </select>
        <div className="relative">
          <Icon name="search" size={14} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-py-muted" />
          <input
            type="search"
            aria-label="Filter runs by name, group or tag"
            placeholder="Filter by name, group or tag"
            value={filters.query}
            onChange={(e) => setFilter({ query: e.target.value })}
            className={`${field} pl-7`}
          />
        </div>
        <div className="flex items-center gap-2">
          <label className="flex min-w-0 flex-1 items-center gap-2 text-xs text-py-muted">
            Sort
            <select
              aria-label="Sort runs"
              value={sort}
              onChange={(e) => {
                const s = e.target.value as RunSort
                setSort(s)
                save('pyro.sidebar.sort', s)
              }}
              className={`${field} h-7 min-w-0 flex-1 text-xs`}
            >
              <option value="updated">Last updated</option>
              <option value="created">Created</option>
              <option value="name">Name</option>
            </select>
          </label>
          <button
            onClick={() => setShowFilters((v) => !v)}
            aria-expanded={showFilters}
            className={`${smallBtn} flex items-center gap-1.5 ${activeFilterCount ? 'border-py-soft text-py-text' : ''}`}
          >
            <Icon name="filter" size={12} />
            Filters{activeFilterCount ? <span className="num"> {activeFilterCount}</span> : null}
          </button>
        </div>
        {showFilters && (
          <div className="flex max-h-[45vh] flex-col gap-2 overflow-y-auto" role="group" aria-label="Run filters">
            <select aria-label="Status" value={filters.status} onChange={(e) => setFilter({ status: e.target.value })} className={field}>
              <option value="">Any status</option>
              {STATUSES.map((s) => <option key={s} value={s}>{s}{facets?.statuses[s] ? ` (${facets.statuses[s]})` : ''}</option>)}
            </select>
            <select aria-label="Group" value={filters.group} onChange={(e) => setFilter({ group: e.target.value })} className={field}>
              <option value="">Any group</option>
              {groupChoices.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
            <div className="flex items-center gap-1">
              <input type="date" aria-label="Created from" value={filters.from} onChange={(e) => setFilter({ from: e.target.value })} className={`${field} min-w-0 flex-1`} />
              <span className="text-xs text-py-faint">to</span>
              <input type="date" aria-label="Created to" value={filters.to} onChange={(e) => setFilter({ to: e.target.value })} className={`${field} min-w-0 flex-1`} />
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
                      className={`rounded-[2px] border px-1.5 py-0.5 text-xs transition-colors duration-150 ${
                        on ? 'border-py-border-hi bg-py-elevated text-py-text' : 'border-py-border text-py-muted hover:bg-py-surface hover:text-py-text'
                      }`}
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
        {serverDown && <div className="px-4 py-3 text-xs text-py-red">Server unreachable</div>}
        {!serverDown && !loading && visible.length === 0 && visiblePins.length === 0 && (
          <div className="px-4 py-4 text-xs text-py-muted">
            {filters.query || activeFilters ? 'No matching runs' : <>No runs yet. Start one with <code className="num text-py-soft">pyrometer.init()</code>.</>}
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
              {loading ? 'Loading' : <>Load more (<span className="num">{Math.max(total - offsetRef.current, 0)}</span> left)</>}
            </button>
          </div>
        )}
      </div>

      {/* Footer: selection / bulk bar */}
      <div className="border-t border-py-border">
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
            <div className="flex items-center gap-1.5 p-2">
              <button
                onClick={() => navigate(`/compare?ids=${selectedIds.join(',')}`)}
                disabled={!isComparing}
                className="flex h-8 flex-1 items-center justify-center gap-1.5 rounded-[2px] bg-py-n8 px-3 text-[13px] font-medium text-py-base transition-colors duration-150 hover:bg-py-text disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Icon name="compare" size={14} />
                {isComparing ? <>Compare (<span className="num">{selectedIds.length}</span>)</> : <>Select one more</>}
              </button>
              {isAdmin && (
                <button onClick={() => setBulkOpen((v) => !v)} aria-expanded={bulkOpen} className={`${smallBtn} h-8`}>
                  Bulk
                </button>
              )}
              <button onClick={onClearSelection} title="Clear selection" aria-label="Clear selection" className={`${smallBtn} flex h-8 items-center px-2`}>
                <Icon name="x" size={14} />
              </button>
            </div>
          </>
        ) : (
          <div className="num px-4 py-2 text-[11px] text-py-muted">
            {total} run{total !== 1 ? 's' : ''}{runs.length < total && ` · ${runs.length} loaded`}
          </div>
        )}
      </div>

      <div className="flex items-center justify-between border-t border-py-border px-4 py-2.5">
        <NavLink to="/settings" className={quietLink}>
          <Icon name="settings" size={14} />
          Settings
        </NavLink>
        <button onClick={toggleTheme} className={quietLink} title="Switch light / dark theme">
          <Icon name="sun" size={14} />
          Theme
        </button>
        <button onClick={() => api.logout().finally(() => window.location.reload())} className={quietLink}>
          <Icon name="x" size={14} />
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
    <div className="flex flex-col gap-2 border-b border-py-border p-3" role="group" aria-label="Bulk actions">
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
        <button className={`${smallBtn} ml-auto border-py-red text-py-red hover:text-py-red`} disabled={busy} onClick={() => setConfirmDelete(true)}>Delete…</button>
      </div>
      <div className="flex items-center gap-1">
        <select aria-label="Move to project" value={target} onChange={(e) => setTarget(e.target.value)} className={field}>
          <option value="">Move to…</option>
          {projects.filter((p) => !p.archived).map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
        </select>
        <label className="flex items-center gap-1 text-xs text-py-muted whitespace-nowrap" title="Needed for runs that are still running">
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

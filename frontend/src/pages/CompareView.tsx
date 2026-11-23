import { RUN_COLORS } from '../utils/runColors.ts'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client.ts'
import type { Metric, ProfileEvent, Run } from '../api/types.ts'
import type { XZoom } from '../utils/series.ts'
import ChartDefaults from '../components/ChartDefaults.tsx'
import CompareChart from '../components/CompareChart.tsx'
import CompareOperatorBreakdown from '../components/CompareOperatorBreakdown.tsx'
import HyperparamDiff from '../components/HyperparamDiff.tsx'
import Icon from '../components/Icon.tsx'
import KernelTimeline from '../components/KernelTimeline.tsx'
import SamplesViewer from '../components/SamplesViewer.tsx'
import StatusBadge from '../components/StatusBadge.tsx'
import type { XSource } from '../utils/xAxis.ts'
import {
  decodeChartDefaults,
  decodeIds,
  sameChartDefaults,
  useCompareState,
  withChartDefaults,
  withCompareState,
} from '../utils/compareState.ts'
import { setChartDefaults, useChartDefaults } from '../utils/chartOptions.ts'
import { maxPointsForWidth } from '../utils/series.ts'
import type { RangeQuery } from '../utils/series.ts'
import { groupRuns, parseIgnoreKeys } from '../utils/grouping.ts'
import type { BandKind, GroupMode } from '../utils/grouping.ts'
import { duration, timeAgo } from '../utils/format.ts'

function SectionLabel({ children }: { children: React.ReactNode }) {
  return <h2 className="mb-3 text-[15px] font-semibold text-py-text">{children}</h2>
}

const fieldCls = 'h-8 rounded border border-py-border-hi bg-py-elevated px-2 text-[13px] text-py-text placeholder:text-py-faint'
const chipCls =
  'flex cursor-pointer select-none items-center gap-2 rounded border border-py-border-hi bg-py-surface px-3 py-1.5 text-[13px] text-py-text transition-colors hover:bg-py-elevated'

export default function CompareView() {
  const [searchParams, setSearchParams] = useSearchParams()
  const runIds = useMemo(() => decodeIds(searchParams), [searchParams])

  const [runs, setRuns] = useState<Run[]>([])
  const [metricsMap, setMetricsMap] = useState<Map<string, Metric[]>>(new Map())
  const [thinned, setThinned] = useState<Set<string>>(new Set()) // runs whose metrics the server downsampled
  const [profileMap, setProfileMap] = useState<Map<string, ProfileEvent[]>>(new Map())
  const [globalHiddenIds, setGlobalHiddenIds] = useState<Set<string>>(new Set())
  const [compareState, setCompareState] = useCompareState()
  const { groupMode, ignoreText, band } = compareState
  const compareStateRef = useRef(compareState)
  compareStateRef.current = compareState
  const [profilerOpen, setProfilerOpen] = useState(false)
  // Zoom: linked (default) = dragging on one chart zooms every chart to the same x window.
  const [linkZoom, setLinkZoom] = useState(true)
  const [sharedZoom, setSharedZoom] = useState<XZoom | null>(null)
  const linkedZoom = useMemo(() => (linkZoom ? { zoom: sharedZoom, set: setSharedZoom } : undefined), [linkZoom, sharedZoom])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const runIdsKey = runIds.join(',')

  // The URL is the source of truth for grouping and chart defaults; make it complete (so it can be shared)
  // and keep the chart defaults (a global, localStorage-backed setting) and the URL in step both ways.
  const chartDefaults = useChartDefaults()
  const pendingApply = useRef(false)
  useEffect(() => {
    const fromUrl = decodeChartDefaults(searchParams)
    if (fromUrl && !sameChartDefaults(fromUrl, chartDefaults)) {
      pendingApply.current = true
      setChartDefaults(fromUrl)
    }
    let next = searchParams
    if (!fromUrl) next = withChartDefaults(next, chartDefaults)
    if (!next.has('group')) next = withCompareState(next, compareStateRef.current)
    if (next !== searchParams) setSearchParams(next, { replace: true })
    // chartDefaults is read on purpose without being a dependency: this reacts to URL changes only
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])
  useEffect(() => {
    if (pendingApply.current) {
      pendingApply.current = false
      return
    }
    const fromUrl = decodeChartDefaults(searchParams)
    if (!fromUrl || !sameChartDefaults(fromUrl, chartDefaults)) {
      setSearchParams((prev) => withChartDefaults(prev, chartDefaults), { replace: true })
    }
    // only a change of the defaults themselves writes the URL
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartDefaults])

  /** Charts are at most ~1100px wide here: about two points per pixel from the server. */
  const pageMax = useMemo(() => maxPointsForWidth(Math.min(Math.max(window.innerWidth - 320, 380), 1100)), [])
  const fetchRange = useCallback(
    async (runId: string, name: string, q: RangeQuery, maxPoints: number): Promise<Metric[]> =>
      (await api.getMetricsSeries(runId, name, { ...q, max_points: maxPoints })).data,
    [],
  )

  useEffect(() => {
    if (runIds.length === 0) {
      setLoading(false)
      return
    }

    let mounted = true
    setLoading(true)
    setError(null)

    async function load() {
      try {
        const [runsData, metricsData, profileSteps] = await Promise.all([
          Promise.all(runIds.map((id) => api.getRun(id))),
          Promise.all(runIds.map((id) => api.getMetricsSeries(id, undefined, { max_points: pageMax }))),
          Promise.all(runIds.map((id) => api.getProfileSteps(id))),
        ])
        const profileData = await Promise.all(
          runIds.map((id, i) =>
            profileSteps[i].length > 0
              ? api.getProfileEvents(id, profileSteps[i][0])
              : Promise.resolve([] as ProfileEvent[]),
          ),
        )
        if (!mounted) return
        setRuns(runsData)
        const map = new Map<string, Metric[]>()
        runIds.forEach((id, i) => map.set(id, metricsData[i].data))
        setMetricsMap(map)
        setThinned(new Set(runIds.filter((_, i) => metricsData[i].downsampled)))
        const pmap = new Map<string, ProfileEvent[]>()
        runIds.forEach((id, i) => pmap.set(id, profileData[i]))
        setProfileMap(pmap)
        setLoading(false)
      } catch (e) {
        if (!mounted) return
        setError((e as Error).message)
        setLoading(false)
      }
    }

    load()
    return () => {
      mounted = false
    }
    // runIdsKey is stable string derived from runIds
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runIdsKey])

  // Per-run x-axis sources (each run's metrics by name + start time), so metric axes resolve per run.
  const sources = useMemo(() => {
    const out = new Map<string, XSource>()
    for (const run of runs) {
      const byName: Record<string, Metric[]> = {}
      for (const m of metricsMap.get(run.run_id) ?? []) (byName[m.name] ??= []).push(m)
      for (const k of Object.keys(byName)) byName[k].sort((a, b) => a.step - b.step)
      out.set(run.run_id, { byName, startTime: Date.parse(run.created_at) / 1000, interpolate: thinned.has(run.run_id) })
    }
    return out
  }, [runs, metricsMap, thinned])

  const grouped = groupMode !== 'none'
  const groups = useMemo(
    () => groupRuns(runs, groupMode, parseIgnoreKeys(ignoreText)),
    [runs, groupMode, ignoreText],
  )
  const groupColor = (i: number) => RUN_COLORS[i % RUN_COLORS.length]

  // Group ids are positional, so drop stale group visibility whenever the grouping changes.
  const changeGrouping = (fn: () => void) => {
    fn()
    setGlobalHiddenIds((prev) => new Set([...prev].filter((id) => !id.startsWith('g'))))
  }

  const allMetricNames = useMemo(() => {
    const names = new Set<string>()
    for (const metrics of metricsMap.values()) {
      for (const m of metrics) names.add(m.name)
    }
    return [...names].sort()
  }, [metricsMap])

  if (runIds.length < 2) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="text-[13px] text-py-muted">Select at least 2 runs in the sidebar to compare</span>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="text-[13px] text-py-muted">Loading comparison…</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="text-[13px] text-py-red">{error}</span>
      </div>
    )
  }

  return (
    <div className="p-6 max-w-6xl mx-auto">
      {/* ── Header ── */}
      <div className="mb-4">
        <h1 className="mb-3 text-[22px] font-semibold text-py-text">Compare runs</h1>
        <div className="flex flex-wrap gap-2">
          {grouped && groups.map((g, i) => {
            const hidden = globalHiddenIds.has(g.id)
            return (
              <div
                key={g.id}
                className={`${chipCls} ${hidden ? 'opacity-40' : ''}`}
                onClick={() => setGlobalHiddenIds((prev) => {
                  const next = new Set(prev)
                  if (next.has(g.id)) next.delete(g.id)
                  else next.add(g.id)
                  return next
                })}
                title={hidden ? 'Click to show' : 'Click to hide'}
              >
                <span className="inline-block h-2.5 w-2.5" style={{ backgroundColor: groupColor(i) }} />
                <span>{g.label}</span>
                <span className="text-xs text-py-muted">
                  {g.runIds.length} run{g.runIds.length === 1 ? '' : 's'}
                </span>
              </div>
            )
          })}
          {!grouped && runs.map((run, i) => {
            const hidden = globalHiddenIds.has(run.run_id)
            return (
              <div
                key={run.run_id}
                className={`${chipCls} ${hidden ? 'opacity-40' : ''}`}
                onClick={() => setGlobalHiddenIds((prev) => {
                  const next = new Set(prev)
                  if (next.has(run.run_id)) next.delete(run.run_id)
                  else next.add(run.run_id)
                  return next
                })}
                title={hidden ? 'Click to show' : 'Click to hide'}
              >
                <span className="inline-block h-2.5 w-2.5" style={{ backgroundColor: RUN_COLORS[i % RUN_COLORS.length] }} />
                <span>{run.name}</span>
                <StatusBadge status={run.status} compact />
                <span className="num text-xs text-py-muted">
                  {timeAgo(run.created_at)}
                  {run.finished_at && <> · {duration(run.created_at, run.finished_at)}</>}
                </span>
              </div>
            )
          })}
        </div>
      </div>

      {/* ── Toolbar ── */}
      <div className="mb-6 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2 rounded border border-py-border bg-py-surface px-3 py-1.5">
          <span className="log-label">Group</span>
          <select
            value={groupMode}
            aria-label="Group runs by"
            onChange={(e) => changeGrouping(() => setCompareState({ groupMode: e.target.value as GroupMode }))}
            className={fieldCls}
          >
            <option value="none">none</option>
            <option value="hyperparams">hyperparams</option>
            <option value="tag">tags</option>
          </select>
          {groupMode === 'hyperparams' && (
            <>
              <span className="log-label">Ignore</span>
              <input
                value={ignoreText}
                onChange={(e) => changeGrouping(() => setCompareState({ ignoreText: e.target.value }, true))}
                placeholder="seed"
                aria-label="Hyperparam keys to ignore"
                title="Comma-separated hyperparam keys to ignore when matching runs"
                className={`${fieldCls} w-24 font-mono`}
              />
            </>
          )}
          {grouped && (
            <select
              value={band}
              aria-label="Band"
              onChange={(e) => setCompareState({ band: e.target.value as BandKind })}
              title="Band around the group mean"
              className={fieldCls}
            >
              <option value="std">mean ± std</option>
              <option value="minmax">mean, min/max</option>
            </select>
          )}
        </div>

        <label
          className={`flex h-[42px] cursor-pointer items-center gap-2 rounded border border-py-border bg-py-surface px-3 text-[13px] ${linkZoom ? 'text-py-text' : 'text-py-muted'}`}
          title="When on, zooming one chart zooms every chart to the same x range"
        >
          <input
            type="checkbox"
            checked={linkZoom}
            onChange={(e) => {
              setLinkZoom(e.target.checked)
              setSharedZoom(null)
            }}
          />
          Link zoom
        </label>

        <ChartDefaults axisMetrics={allMetricNames} />
      </div>

      {/* ── Metric overlay charts ── */}
      {allMetricNames.length > 0 && (
        <div className="mb-8">
          <SectionLabel>Metrics</SectionLabel>
          <div
            className="grid gap-4"
            style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 380px), 1fr))' }}
          >
            {allMetricNames.map((name) => (
              <div key={name}>
                <CompareChart
                  metricName={name}
                  series={runs.map((run, i) => ({
                    runId: run.run_id,
                    name: run.name,
                    color: RUN_COLORS[i % RUN_COLORS.length],
                    metrics: sources.get(run.run_id)?.byName?.[name] ?? [],
                    source: sources.get(run.run_id),
                  }))}
                  globalHiddenIds={globalHiddenIds}
                  band={band}
                  fetchRange={fetchRange}
                  linkedZoom={linkedZoom}
                  groups={
                    grouped
                      ? groups.map((g, gi) => ({
                          id: g.id,
                          name: g.label,
                          color: groupColor(gi),
                          runs: g.runIds.flatMap((id) => {
                            const run = runs.find((r) => r.run_id === id)
                            return run
                              ? [{
                                  runId: id,
                                  name: run.name,
                                  color: groupColor(gi),
                                  metrics: sources.get(id)?.byName?.[name] ?? [],
                                  source: sources.get(id),
                                }]
                              : []
                          }),
                        }))
                      : undefined
                  }
                />
              </div>
            ))}
          </div>
        </div>
      )}

      {allMetricNames.length === 0 && (
        <div className="mb-8 py-10 text-center text-[13px] text-py-muted">No metrics logged for these runs</div>
      )}

      {/* ── Profiler ── */}
      {runs.some((r) => (profileMap.get(r.run_id) ?? []).length > 0) && (
        <div className="mb-8">
          <button
            className="mb-3 flex w-full items-center gap-2 text-left text-py-text hover:text-py-text"
            aria-expanded={profilerOpen}
            onClick={() => setProfilerOpen((o) => !o)}
          >
            <h2 className="text-[15px] font-semibold">Profiler</h2>
            <Icon name="chevron-down" size={14} className={`text-py-muted ${profilerOpen ? 'rotate-180' : ''}`} />
          </button>
          {profilerOpen && (
            <div className="flex flex-col gap-6">
              {runs.map((run, i) => {
                const events = profileMap.get(run.run_id) ?? []
                if (events.length === 0) return null
                return (
                  <div key={run.run_id}>
                    <div className="mb-2 flex items-center gap-2 text-[13px] text-py-soft">
                      <span className="inline-block h-2.5 w-2.5" style={{ backgroundColor: RUN_COLORS[i % RUN_COLORS.length] }} />
                      {run.name}
                    </div>
                    <KernelTimeline events={events} />
                  </div>
                )
              })}
              <CompareOperatorBreakdown
                series={runs.map((run, i) => ({
                  runId: run.run_id,
                  name: run.name,
                  color: RUN_COLORS[i % RUN_COLORS.length],
                  events: profileMap.get(run.run_id) ?? [],
                }))}
              />
            </div>
          )}
        </div>
      )}

      {/* ── Samples (nothing rendered if no run logged any) ── */}
      <SamplesViewer runs={runs.map((r, i) => ({ runId: r.run_id, label: r.name, color: RUN_COLORS[i % RUN_COLORS.length] }))} />

      {/* ── Hyperparameter diff ── */}
      <div>
        <SectionLabel>Hyperparameter diff</SectionLabel>
        <HyperparamDiff
          runs={runs}
          colors={runs.map((_, i) => RUN_COLORS[i % RUN_COLORS.length])}
        />
      </div>
    </div>
  )
}

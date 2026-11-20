import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client.ts'
import type { Metric, ProfileEvent, Run } from '../api/types.ts'
import CompareChart from '../components/CompareChart.tsx'
import CompareOperatorBreakdown from '../components/CompareOperatorBreakdown.tsx'
import HyperparamDiff from '../components/HyperparamDiff.tsx'
import KernelTimeline from '../components/KernelTimeline.tsx'
import SamplesViewer from '../components/SamplesViewer.tsx'
import StatusBadge from '../components/StatusBadge.tsx'
import type { XSource } from '../utils/xAxis.ts'
import { groupRuns, parseIgnoreKeys } from '../utils/grouping.ts'
import type { BandKind, GroupMode } from '../utils/grouping.ts'
import { duration, timeAgo } from '../utils/format.ts'

export const RUN_COLORS = [
  '#f59e0b', // amber
  '#22d3ee', // cyan
  '#f43f5e', // rose
  '#8b5cf6', // violet
  '#84cc16', // lime
  '#fb923c', // orange
]

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <h2
      className="text-xs font-mono uppercase mb-3"
      style={{ color: '#c8cad6', letterSpacing: '0.12em' }}
    >
      {children}
    </h2>
  )
}

export default function CompareView() {
  const [searchParams] = useSearchParams()
  const runIds = useMemo(
    () => (searchParams.get('ids') ?? '').split(',').filter(Boolean),
    [searchParams],
  )

  const [runs, setRuns] = useState<Run[]>([])
  const [metricsMap, setMetricsMap] = useState<Map<string, Metric[]>>(new Map())
  const [profileMap, setProfileMap] = useState<Map<string, ProfileEvent[]>>(new Map())
  const [smoothing, setSmoothing] = useState(0)
  const [globalHiddenIds, setGlobalHiddenIds] = useState<Set<string>>(new Set())
  const [groupMode, setGroupMode] = useState<GroupMode>('none')
  const [ignoreText, setIgnoreText] = useState('seed')
  const [band, setBand] = useState<BandKind>('std')
  const [profilerOpen, setProfilerOpen] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const runIdsKey = runIds.join(',')

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
          Promise.all(runIds.map((id) => api.getMetrics(id))),
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
        runIds.forEach((id, i) => map.set(id, metricsData[i]))
        setMetricsMap(map)
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
      out.set(run.run_id, { byName, startTime: Date.parse(run.created_at) / 1000 })
    }
    return out
  }, [runs, metricsMap])

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
      <div className="flex items-center justify-center h-full">
        <span className="text-sm font-mono" style={{ color: '#4a4e60' }}>
          Select at least 2 runs in the sidebar to compare
        </span>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <span className="text-sm font-mono" style={{ color: '#4a4e60' }}>
          Loading comparison…
        </span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-full">
        <span className="text-sm font-mono" style={{ color: '#ef4444' }}>
          {error}
        </span>
      </div>
    )
  }

  return (
    <div className="p-6 max-w-6xl mx-auto">
      {/* ── Header ── */}
      <div className="flex items-start justify-between mb-6 gap-4 flex-wrap">
        <div>
          <div
            className="text-xs font-mono uppercase mb-3"
            style={{ color: '#c8cad6', letterSpacing: '0.12em' }}
          >
            Run Comparison
          </div>
          {/* Run pills */}
          <div className="flex flex-wrap gap-2">
            {grouped && groups.map((g, i) => {
              const hidden = globalHiddenIds.has(g.id)
              return (
                <div
                  key={g.id}
                  className="flex items-center gap-2 text-xs font-mono px-3 py-1.5 rounded cursor-pointer select-none"
                  style={{
                    backgroundColor: '#111318',
                    border: `1px solid ${groupColor(i)}33`,
                    color: groupColor(i),
                    opacity: hidden ? 0.35 : 1,
                  }}
                  onClick={() => setGlobalHiddenIds((prev) => {
                    const next = new Set(prev)
                    if (next.has(g.id)) next.delete(g.id)
                    else next.add(g.id)
                    return next
                  })}
                  title={hidden ? 'Click to show' : 'Click to hide'}
                >
                  <span style={{ display: 'inline-block', width: 20, height: 2, backgroundColor: groupColor(i), borderRadius: 1 }} />
                  <span>{g.label}</span>
                  <span style={{ color: '#4a4e60' }}>
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
                className="flex items-center gap-2 text-xs font-mono px-3 py-1.5 rounded cursor-pointer select-none"
                style={{
                  backgroundColor: '#111318',
                  border: `1px solid ${RUN_COLORS[i % RUN_COLORS.length]}33`,
                  color: RUN_COLORS[i % RUN_COLORS.length],
                  opacity: hidden ? 0.35 : 1,
                }}
                onClick={() => setGlobalHiddenIds((prev) => {
                  const next = new Set(prev)
                  if (next.has(run.run_id)) next.delete(run.run_id)
                  else next.add(run.run_id)
                  return next
                })}
                title={hidden ? 'Click to show' : 'Click to hide'}
              >
                <span
                  style={{
                    display: 'inline-block',
                    width: 20,
                    height: 2,
                    backgroundColor: RUN_COLORS[i % RUN_COLORS.length],
                    borderRadius: 1,
                  }}
                />
                <span>{run.name}</span>
                <span style={{ opacity: 0.5 }}>·</span>
                <StatusBadge status={run.status} compact />
                <span style={{ color: '#4a4e60' }}>
                  {timeAgo(run.created_at)}
                  {run.finished_at && <> · {duration(run.created_at, run.finished_at)}</>}
                </span>
              </div>
              )
            })}
          </div>
        </div>

        <div className="flex items-center gap-3 flex-wrap">
        {/* Grouping */}
        <div
          className="flex items-center gap-2 px-3 py-2 rounded text-xs font-mono"
          style={{ backgroundColor: '#111318', border: '1px solid #252830', color: '#4a4e60' }}
        >
          <span>Group</span>
          <select
            value={groupMode}
            onChange={(e) => changeGrouping(() => setGroupMode(e.target.value as GroupMode))}
            style={{ backgroundColor: '#1a1d24', color: '#c8cad6', border: '1px solid #353840', borderRadius: 4 }}
          >
            <option value="none">none</option>
            <option value="hyperparams">hyperparams</option>
            <option value="tag">tags</option>
          </select>
          {groupMode === 'hyperparams' && (
            <>
              <span>ignore</span>
              <input
                value={ignoreText}
                onChange={(e) => changeGrouping(() => setIgnoreText(e.target.value))}
                placeholder="seed"
                title="Comma-separated hyperparam keys to ignore when matching runs"
                style={{ width: 90, backgroundColor: '#1a1d24', color: '#c8cad6', border: '1px solid #353840', borderRadius: 4, padding: '0 4px' }}
              />
            </>
          )}
          {grouped && (
            <select
              value={band}
              onChange={(e) => setBand(e.target.value as BandKind)}
              title="Band around the group mean"
              style={{ backgroundColor: '#1a1d24', color: '#c8cad6', border: '1px solid #353840', borderRadius: 4 }}
            >
              <option value="std">mean ± std</option>
              <option value="minmax">mean, min/max</option>
            </select>
          )}
        </div>

        {/* Smoothing slider */}
        <div
          className="flex items-center gap-3 px-3 py-2 rounded"
          style={{ backgroundColor: '#111318', border: '1px solid #252830' }}
        >
          <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>
            Smooth
          </span>
          <input
            type="range"
            min={0}
            max={0.95}
            step={0.05}
            value={smoothing}
            onChange={(e) => setSmoothing(parseFloat(e.target.value))}
            style={{ width: 96, accentColor: '#f59e0b', cursor: 'pointer' }}
          />
          <span
            className="text-xs font-mono w-8 text-right"
            style={{ color: smoothing > 0 ? '#f59e0b' : '#4a4e60' }}
          >
            {smoothing.toFixed(2)}
          </span>
        </div>
        </div>
      </div>

      {/* ── Metric overlay charts ── */}
      {allMetricNames.length > 0 && (
        <div className="mb-8">
          <SectionLabel>Metrics</SectionLabel>
          <div
            className="grid gap-4"
            style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(380px, 1fr))' }}
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
                  smoothing={smoothing}
                  globalHiddenIds={globalHiddenIds}
                  band={band}
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
        <div
          className="text-center py-10 text-sm font-mono mb-8"
          style={{ color: '#4a4e60' }}
        >
          No metrics logged for these runs
        </div>
      )}

      {/* ── Profiler ── */}
      {runs.some((r) => (profileMap.get(r.run_id) ?? []).length > 0) && (
        <div className="mb-8">
          <button
            className="flex items-center gap-2 mb-3 w-full text-left"
            onClick={() => setProfilerOpen((o) => !o)}
          >
            <h2
              className="text-xs font-mono uppercase"
              style={{ color: '#c8cad6', letterSpacing: '0.12em' }}
            >
              Profiler
            </h2>
            <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>
              {profilerOpen ? '▲' : '▼'}
            </span>
          </button>
          {profilerOpen && (
            <div className="flex flex-col gap-6">
              {runs.map((run, i) => {
                const events = profileMap.get(run.run_id) ?? []
                if (events.length === 0) return null
                return (
                  <div key={run.run_id}>
                    <div
                      className="text-xs font-mono mb-2"
                      style={{ color: RUN_COLORS[i % RUN_COLORS.length] }}
                    >
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
        <div className="flex items-center gap-3 mb-3">
          <SectionLabel>Hyperparameter Diff</SectionLabel>
          <span
            className="text-xs font-mono -mt-3"
            style={{ color: '#f59e0b' }}
          >
            ◆ differs
          </span>
        </div>
        <HyperparamDiff
          runs={runs}
          colors={runs.map((_, i) => RUN_COLORS[i % RUN_COLORS.length])}
        />
      </div>
    </div>
  )
}

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../api/client.ts'
import type { Artifact, Metric, ParamStat, ProfileEvent, Run, RunEvent, RunLog, RunSummary, SystemSnapshot } from '../api/types.ts'
import RunHeader from '../components/run/RunHeader.tsx'
import RunTabs, { panelDomId, tabDomId } from '../components/run/RunTabs.tsx'
import OverviewTab from '../components/run/OverviewTab.tsx'
import MetricsTab from '../components/run/MetricsTab.tsx'
import type { MetricRange } from '../components/run/MetricsTab.tsx'
import SystemTab from '../components/run/SystemTab.tsx'
import HealthTab from '../components/run/HealthTab.tsx'
import ProfilerTab from '../components/run/ProfilerTab.tsx'
import { ArtifactsTab, LogsTab, MediaTab, NotesTab } from '../components/run/OtherTabs.tsx'
import { Skeleton, TAB_LABEL, TAB_ORDER } from '../components/run/shared.tsx'
import type { TabId } from '../components/run/shared.tsx'
import ViewSettings from '../components/ViewSettings.tsx'
import { useSession } from '../hooks/useSession.ts'
import { useWebSocket } from '../hooks/useWebSocket.ts'
import { timeWindow } from '../utils/xAxis.ts'
import type { XSource } from '../utils/xAxis.ts'
import { backfillTargets, budgetGrew, mergeBuckets, needsResample, tailStart, zoomQuery } from '../utils/series.ts'
import { chartPointsBudget, onChartWidthChange } from '../hooks/useMeasuredWidth.ts'
import type { RangeQuery, XZoom } from '../utils/series.ts'
import type { ExternalZoom } from '../components/MetricChart.tsx'
import { defaultPins, hidePattern, isPinned, resolvePins, setTabHidden, showPattern, togglePin, useViewConfig } from '../utils/viewConfig.ts'

/** Series budget: about two points per pixel of the widest chart on screen (measured with a ResizeObserver);
 * before any chart is mounted, an estimate from the page width. */
function pagePoints(): number {
  return chartPointsBudget(Math.min(Math.max(window.innerWidth - 320, 320), 1000))
}

const metricKey = (m: Metric) => `${m.name}\u0000${m.step}`
const metricOrder = (a: Metric, b: Metric) => (a.name < b.name ? -1 : a.name > b.name ? 1 : a.step - b.step)
const snapKey = (s: SystemSnapshot) => `${s.gpu_index ?? 0}\u0000${s.timestamp}`
const snapOrder = (a: SystemSnapshot, b: SystemSnapshot) => a.timestamp - b.timestamp || (a.gpu_index ?? 0) - (b.gpu_index ?? 0)
const statKey = (p: ParamStat) => `${p.param_name}\u0000${p.step}`
const statOrder = (a: ParamStat, b: ParamStat) => a.timestamp - b.timestamp || (a.param_name < b.param_name ? -1 : 1)

function RunDetailSkeleton() {
  return (
    <div className="mx-auto max-w-6xl p-6" aria-busy="true" aria-label="Loading run">
      <div className="mb-4 flex items-start justify-between">
        <div className="flex flex-col gap-2">
          <Skeleton h={26} w={240} />
          <Skeleton h={14} w={160} />
        </div>
        <Skeleton h={28} w={260} />
      </div>
      <div className="mb-5 flex gap-4 border-b border-py-border pb-2.5">
        {[56, 56, 52, 88, 56, 44].map((w, i) => <Skeleton key={i} h={14} w={w} />)}
      </div>
      <div className="mb-5"><Skeleton h={136} /></div>
      <div className="mb-5"><Skeleton h={64} /></div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Skeleton h={220} />
        <Skeleton h={220} />
      </div>
    </div>
  )
}

export default function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const { session, isAdmin } = useSession()
  const canPin = session?.role === 'admin' || session?.role === 'user'
  const [deleted, setDeleted] = useState(false)
  const [run, setRun] = useState<Run | null>(null)
  const [summary, setSummary] = useState<RunSummary | null>(null)
  const [metrics, setMetrics] = useState<Metric[]>([])
  const [snapshots, setSnapshots] = useState<SystemSnapshot[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Profiler state
  const [profileSteps, setProfileSteps] = useState<number[]>([])
  const [selectedStep, setSelectedStep] = useState<number | null>(null)
  const [profileEvents, setProfileEvents] = useState<ProfileEvent[]>([])
  const [profileLoading, setProfileLoading] = useState(false)

  // Model health state
  const [paramStats, setParamStats] = useState<ParamStat[]>([])
  const [runEvents, setRunEvents] = useState<RunEvent[]>([])

  // Global event marker toggle (default on)
  const [showEvents, setShowEvents] = useState(true)

  // Console logs & artifacts
  const [logs, setLogs] = useState<RunLog[]>([])
  const [artifacts, setArtifacts] = useState<Artifact[]>([])

  // Series are downsampled by the server; live updates fetch only the new tail and merge it in.
  const [metricsDownsampled, setMetricsDownsampled] = useState(false)
  const [liveTick, setLiveTick] = useState(0) // bumps when new metrics/system/stats arrive (refreshes zoomed windows)
  const [gpuZoom, setGpuZoom] = useState<{ t_min: number; t_max: number } | null>(null)
  const [gpuZoomSnaps, setGpuZoomSnaps] = useState<SystemSnapshot[] | null>(null)
  const metricsRef = useRef<Metric[]>([])
  const snapsRef = useRef<SystemSnapshot[]>([])
  const statsRef = useRef<ParamStat[]>([])
  const locks = useRef<Record<string, Promise<unknown>>>({})
  const loadedPoints = useRef(0) // the max_points the held series were last loaded with
  /** Serialize refreshes of one slice so two overlapping tail fetches cannot overwrite each other's merge. */
  const serial = useCallback((key: string, fn: () => Promise<void>): Promise<void> => {
    const next = (locks.current[key] ?? Promise.resolve()).then(fn, fn)
    locks.current[key] = next.catch(() => {})
    return next
  }, [])

  // Stable refs so WebSocket callbacks never go stale
  const runRef = useRef<Run | null>(null)
  const mountedRef = useRef(false)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Individual targeted fetch functions — each only refreshes its own slice of state
  const fetchRun = useCallback(async () => {
    if (!runId || !mountedRef.current) return
    const data = await api.getRun(runId)
    if (!mountedRef.current) return
    runRef.current = data
    setRun(data)
  }, [runId])

  const loadMetrics = useCallback(async () => {
    const r = await api.getMetricsSeries(runId!, undefined, { max_points: (loadedPoints.current = pagePoints()) })
    if (!mountedRef.current) return
    metricsRef.current = r.data
    setMetrics(r.data)
    setMetricsDownsampled(r.downsampled)
  }, [runId])

  const fetchMetrics = useCallback(() => serial('metrics', async () => {
    if (!runId || !mountedRef.current) return
    const held = metricsRef.current
    const t = tailStart(held)
    if (t === undefined) return loadMetrics()
    const r = await api.getMetricsSeries(runId, undefined, { t_min: t })
    if (!mountedRef.current) return
    if (r.data.length === 0) return
    const merged = mergeBuckets(held, r.data, metricKey, metricOrder)
    if (needsResample(merged.length, new Set(merged.map((m) => m.name)).size, pagePoints())) return loadMetrics()
    metricsRef.current = merged
    setMetrics(merged)
    setLiveTick((n) => n + 1)
  }), [runId, serial, loadMetrics])

  const loadSystem = useCallback(async () => {
    const r = await api.getSystemSeries(runId!, { max_points: (loadedPoints.current = pagePoints()) })
    if (!mountedRef.current) return
    snapsRef.current = r.data
    setSnapshots(r.data)
  }, [runId])

  const fetchSystem = useCallback(() => serial('system', async () => {
    if (!runId || !mountedRef.current) return
    const held = snapsRef.current
    const t = tailStart(held)
    if (t === undefined) return loadSystem()
    const r = await api.getSystemSeries(runId, { t_min: t })
    if (!mountedRef.current || r.data.length === 0) return
    const merged = mergeBuckets(held, r.data, snapKey, snapOrder)
    if (needsResample(merged.length, new Set(merged.map((s) => s.gpu_index ?? 0)).size, pagePoints())) return loadSystem()
    snapsRef.current = merged
    setSnapshots(merged)
    setLiveTick((n) => n + 1)
  }), [runId, serial, loadSystem])

  const loadParamStats = useCallback(async () => {
    const r = await api.getParamStatsSeries(runId!, { max_points: (loadedPoints.current = pagePoints()) })
    if (!mountedRef.current) return
    statsRef.current = r.data
    setParamStats(r.data)
  }, [runId])

  const fetchParamStats = useCallback(() => serial('stats', async () => {
    if (!runId || !mountedRef.current) return
    const held = statsRef.current
    const t = tailStart(held)
    if (t === undefined) return loadParamStats()
    const r = await api.getParamStatsSeries(runId, { t_min: t })
    if (!mountedRef.current || r.data.length === 0) return
    const merged = mergeBuckets(held, r.data, statKey, statOrder)
    if (needsResample(merged.length, new Set(merged.map((s) => s.param_name)).size, pagePoints())) return loadParamStats()
    statsRef.current = merged
    setParamStats(merged)
    setLiveTick((n) => n + 1)
  }), [runId, serial, loadParamStats])

  const fetchEvents = useCallback(async () => {
    if (!runId || !mountedRef.current) return
    const data = await api.getEvents(runId)
    if (!mountedRef.current) return
    setRunEvents(data)
  }, [runId])

  const fetchLogs = useCallback(async () => {
    if (!runId || !mountedRef.current) return
    const data = await api.getLogs(runId)
    if (!mountedRef.current) return
    setLogs(data)
  }, [runId])

  const fetchArtifacts = useCallback(async () => {
    if (!runId || !mountedRef.current) return
    const data = await api.getArtifacts(runId)
    if (!mountedRef.current) return
    setArtifacts(data)
  }, [runId])

  const fetchSummary = useCallback(async () => {
    if (!runId || !mountedRef.current) return
    const data = await api.getRunSummary(runId).catch(() => null)
    if (!mountedRef.current) return
    setSummary(data)
  }, [runId])

  useEffect(() => {
    if (!runId) return
    mountedRef.current = true

    async function initialLoad() {
      try {
        const pts = (loadedPoints.current = pagePoints())
        const [runData, metricsData, snapshotsData, paramStatsData, eventsData, logsData, artifactsData, summaryData] = await Promise.all([
          api.getRun(runId!),
          api.getMetricsSeries(runId!, undefined, { max_points: pts }),
          api.getSystemSeries(runId!, { max_points: pts }),
          api.getParamStatsSeries(runId!, { max_points: pts }),
          api.getEvents(runId!),
          api.getLogs(runId!),
          api.getArtifacts(runId!),
          api.getRunSummary(runId!).catch(() => null),
        ])
        if (!mountedRef.current) return
        runRef.current = runData
        setRun(runData)
        metricsRef.current = metricsData.data
        snapsRef.current = snapshotsData.data
        statsRef.current = paramStatsData.data
        setMetrics(metricsData.data)
        setMetricsDownsampled(metricsData.downsampled)
        setSnapshots(snapshotsData.data)
        setParamStats(paramStatsData.data)
        setRunEvents(eventsData)
        setLogs(logsData)
        setArtifacts(artifactsData)
        setSummary(summaryData)
        setLoading(false)

        // Only poll for live runs — finished runs never change. A crashed run keeps polling
        // because a late heartbeat revives it to running.
        if (runData.status === 'running' || runData.status === 'crashed') {
          intervalRef.current = setInterval(async () => {
            await Promise.all([
              fetchRun(), fetchMetrics(), fetchSystem(), fetchParamStats(),
              fetchEvents(), fetchLogs(), fetchArtifacts(),
            ])
          }, 5000)
        }
      } catch (e) {
        if (!mountedRef.current) return
        setError((e as Error).message)
        setLoading(false)
      }
    }

    setLoading(true)
    setError(null)
    setGpuZoom(null)
    initialLoad()

    return () => {
      mountedRef.current = false
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }
  }, [runId, fetchRun, fetchMetrics, fetchSystem, fetchParamStats, fetchEvents, fetchLogs, fetchArtifacts])

  // Late data (a replayed spool) carries timestamps older than the tail we merge live, so the tail refetch
  // would never see it: the server says so with run_backfill and the affected series are reloaded in full.
  const reloadBackfilled = useCallback((kind?: string) => {
    const loaders = { metrics: loadMetrics, system: loadSystem, stats: loadParamStats }
    const jobs = backfillTargets(kind).map((k) => serial(k, loaders[k]))
    Promise.all(jobs).then(() => setLiveTick((n) => n + 1)).catch(() => {})
  }, [serial, loadMetrics, loadSystem, loadParamStats])

  // Charts resized (window, sidebar, layout): refetch at the new resolution once the budget clearly outgrew the load.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const off = onChartWidthChange(() => {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        if (mountedRef.current && loadedPoints.current > 0 && budgetGrew(loadedPoints.current, pagePoints())) reloadBackfilled()
      }, 400)
    })
    return () => {
      off()
      if (timer) clearTimeout(timer)
    }
  }, [reloadBackfilled])

  // React immediately to server push events — each event type only refetches its own data
  useWebSocket(
    useCallback(
      (msg) => {
        if (msg.type === 'project_deleted') {
          if ((msg as { project?: string }).project === runRef.current?.project) setDeleted(true)
          return
        }
        if (msg.type === 'project_updated') {
          fetchRun().catch(() => {})
          return
        }
        if (msg.run_id !== runId) return
        if (msg.type === 'run_deleted') setDeleted(true)
        else if (msg.type === 'run_moved') fetchRun().catch(() => setDeleted(true))
        else if (msg.type === 'metrics') fetchMetrics()
        else if (msg.type === 'system') fetchSystem()
        else if (msg.type === 'param_stats') fetchParamStats()
        else if (msg.type === 'run_backfill') reloadBackfilled((msg as { kind?: string }).kind)
        else if (msg.type === 'logs') fetchLogs()
        else if (msg.type === 'run_updated') {
          fetchRun().then(() => {
            const status = runRef.current?.status
            if (status && status !== 'running' && status !== 'crashed') {
              // Run just finished — clear poll interval and grab final summary
              if (intervalRef.current) {
                clearInterval(intervalRef.current)
                intervalRef.current = null
              }
              fetchSummary()
            }
          }).catch(() => {})
        }
      },
      [runId, fetchRun, fetchMetrics, fetchSystem, fetchParamStats, fetchLogs, fetchSummary, reloadBackfilled],
    ),
  )

  // A deleted run has nothing left to show: say so, then go back to the start page.
  useEffect(() => {
    if (!deleted) return
    if (intervalRef.current) {
      clearInterval(intervalRef.current)
      intervalRef.current = null
    }
    const t = setTimeout(() => navigate('/'), 2500)
    return () => clearTimeout(t)
  }, [deleted, navigate])

  useEffect(() => setDeleted(false), [runId])

  // Fetch available profiled steps once on mount
  useEffect(() => {
    if (!runId) return
    api.getProfileSteps(runId).then((steps) => {
      setProfileSteps(steps)
      if (steps.length > 0) setSelectedStep(steps[0])
    }).catch(() => {})
  }, [runId])

  // Fetch profile events whenever the selected step changes
  useEffect(() => {
    if (!runId || selectedStep === null) return
    setProfileLoading(true)
    api.getProfileEvents(runId, selectedStep).then((events) => {
      setProfileEvents(events)
      setProfileLoading(false)
    }).catch(() => setProfileLoading(false))
  }, [runId, selectedStep])

  const metricsByName = useMemo(() => {
    const groups: Record<string, Metric[]> = {}
    for (const m of metrics) {
      if (!groups[m.name]) groups[m.name] = []
      groups[m.name].push(m)
    }
    for (const name of Object.keys(groups)) {
      groups[name].sort((a, b) => a.step - b.step)
    }
    return groups
  }, [metrics])

  const sortedSnapshots = useMemo(
    () => [...(gpuZoom && gpuZoomSnaps ? gpuZoomSnaps : snapshots)].sort((a, b) => a.timestamp - b.timestamp),
    [snapshots, gpuZoom, gpuZoomSnaps],
  )
  const gpuIndices = useMemo(
    () => [...new Set(sortedSnapshots.map((s) => s.gpu_index ?? 0))].sort((a, b) => a - b),
    [sortedSnapshots],
  )

  const axisSource = useMemo(
    () => ({
      byName: metricsByName,
      startTime: run ? Date.parse(run.created_at) / 1000 : undefined,
      interpolate: metricsDownsampled,
    }),
    [metricsByName, run, metricsDownsampled],
  )

  /** Zoom refetch for metric charts: one series (or axis metric) over a step/time window at full resolution. */
  const fetchRange = useCallback(
    async (name: string, q: RangeQuery, maxPoints: number): Promise<Metric[]> =>
      (await api.getMetricsSeries(runId!, name, { ...q, max_points: maxPoints })).data,
    [runId],
  )

  // GPU charts share one zoom (they share the time axis); x is elapsed seconds since the first snapshot.
  const gpuT0 = useMemo(() => snapshots.reduce((m, s) => Math.min(m, s.timestamp), Infinity), [snapshots])
  const gpuExternalZoom: ExternalZoom = useMemo(
    () => ({
      active: gpuZoom != null,
      onSelect: (z: XZoom) => setGpuZoom(timeWindow(z.axis, z.x0, z.x1, gpuT0)),
      onReset: () => {
        setGpuZoom(null)
        setGpuZoomSnaps(null)
      },
    }),
    [gpuZoom, gpuT0],
  )
  useEffect(() => {
    if (!gpuZoom || !runId) return
    let live = true
    api.getSystemSeries(runId, { ...gpuZoom, max_points: pagePoints() })
      .then((r) => live && setGpuZoomSnaps(r.data))
      .catch(() => {})
    return () => {
      live = false
    }
  }, [gpuZoom, runId, liveTick])

  const gpuSource = useMemo<XSource>(() => ({ startTime: Number.isFinite(gpuT0) ? gpuT0 : 0 }), [gpuT0])


  // ── Tabs, per-project view, and the time window chosen on the GPU load curve ──
  const [params, setParams] = useSearchParams()
  const view = useViewConfig(run?.project ?? '')

  const [hasMedia, setHasMedia] = useState(false)
  useEffect(() => setHasMedia(false), [runId])
  useEffect(() => {
    if (!runId || hasMedia) return
    let live = true
    Promise.all([api.getMediaNames(runId).catch(() => []), api.getSampleSteps(runId).catch(() => [])]).then(
      ([m, s]) => live && setHasMedia(m.length > 0 || s.length > 0),
    )
    return () => {
      live = false
    }
  }, [runId, hasMedia, liveTick])

  // The firing curve selects a timestamp window. It maps cleanly onto the metrics endpoint (t_min/t_max) because every
  // metric row carries a timestamp, so no time -> step conversion is needed: all series are refetched for the window
  // at full resolution and handed to the Metrics tab's charts (until that arrives, the held series are filtered locally).
  const [range, setRange] = useState<MetricRange | null>(null)
  const [rangeData, setRangeData] = useState<Metric[] | null>(null)
  useEffect(() => {
    setRange(null)
    setRangeData(null)
  }, [runId])
  useEffect(() => {
    if (!range || !runId) {
      setRangeData(null)
      return
    }
    let live = true
    api.getMetricsSeries(runId, undefined, { t_min: range.tMin, t_max: range.tMax, max_points: pagePoints() })
      .then((r) => live && setRangeData(r.data))
      .catch(() => {})
    return () => {
      live = false
    }
  }, [range, runId, liveTick])
  const rangeByName = useMemo(() => {
    if (!range) return null
    const rows = rangeData ?? metrics.filter((m) => m.timestamp >= range.tMin && m.timestamp <= range.tMax)
    const g: Record<string, Metric[]> = {}
    for (const m of rows) (g[m.name] ??= []).push(m)
    for (const k of Object.keys(g)) g[k].sort((a, b) => a.step - b.step)
    return g
  }, [range, rangeData, metrics])
  const startTime = run ? Date.parse(run.created_at) / 1000 : 0
  const rangeZoom: ExternalZoom = useMemo(
    () => ({
      active: true,
      onReset: () => setRange(null),
      onSelect: (z: XZoom) => {
        const q: RangeQuery | null = zoomQuery(z, [], startTime)
        if (!q) return
        if ('t_min' in q) return setRange({ tMin: q.t_min, tMax: q.t_max })
        // A step window: take the timestamps of the held series at those steps.
        let best: Metric[] = []
        for (const arr of Object.values(metricsByName)) if (arr.length > best.length) best = arr
        const inside = best.filter((m) => m.step >= q.step_min && m.step <= q.step_max)
        if (inside.length > 1) setRange({ tMin: inside[0].timestamp, tMax: inside[inside.length - 1].timestamp })
      },
    }),
    [startTime, metricsByName],
  )

  if (deleted) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2" role="alert">
        <span className="text-sm text-py-text">This run was deleted.</span>
        <button onClick={() => navigate('/')} className="text-xs text-py-muted underline hover:text-py-text">Back to runs</button>
      </div>
    )
  }

  if (loading) {
    return <RunDetailSkeleton />
  }

  if (error || !run) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="text-sm text-py-red">{error ?? 'Run not found'}</span>
      </div>
    )
  }

  const metricNames = Object.keys(metricsByName).sort()
  const anomalyCount = runEvents.filter((e) => e.event_type === 'anomaly_detected').length
  const hasMeta = !!(run.gpu_model || run.gpus?.length || run.cuda_version || run.pytorch_version || run.python_version || run.hostname || run.git_hash)
  const has: Record<TabId, boolean> = {
    overview: true,
    metrics: metricNames.length > 0,
    system: snapshots.length > 0 || hasMeta,
    health: paramStats.length > 0 || anomalyCount > 0,
    profiler: profileSteps.length > 0,
    logs: logs.length > 0,
    media: hasMedia,
    artifacts: artifacts.length > 0,
    notes: true,
  }
  const urlTab = params.get('tab') as TabId | null
  const active: TabId = urlTab && TAB_ORDER.includes(urlTab) && has[urlTab] ? urlTab : 'overview'
  // Hidden tabs stay out of the bar, except the one currently open (e.g. reached from the GPU load curve).
  const visibleTabs = TAB_ORDER.filter((id) => has[id] && (id === 'overview' || id === active || !view.cfg.hiddenTabs.includes(id)))
  const setTab = (id: TabId) =>
    setParams((prev) => {
      const n = new URLSearchParams(prev)
      if (id === 'overview') n.delete('tab')
      else n.set('tab', id)
      return n
    })

  const lastStep = metrics.reduce<number | null>((m, x) => (m == null || x.step > m ? x.step : m), null)
  const pinnedNames = resolvePins(view.cfg, metricNames)
  const pinList = view.cfg.pinned ?? defaultPins(metricNames)
  const isPinnedFn = (name: string) => isPinned(name, view.cfg, metricNames)
  const onTogglePin = (name: string) => view.update((c) => togglePin(c, name, metricNames))
  const onHide = (pattern: string) => view.update((c) => hidePattern(c, pattern))
  const onShowPattern = (pattern: string) => view.update((c) => showPattern(c, pattern))
  const onGoTab = (id: TabId) => setTab(has[id] ? id : 'overview')
  const onSelectRange = (tMin: number, tMax: number) => {
    setRange({ tMin, tMax })
    setTab('metrics')
  }

  return (
    <div className="mx-auto max-w-6xl p-6">
      <RunHeader run={run} isAdmin={isAdmin} canPin={canPin} lastStep={lastStep} onChanged={fetchRun} />

      <RunTabs
        tabs={visibleTabs}
        active={active}
        onChange={setTab}
        badges={{ health: anomalyCount }}
        right={
          <ViewSettings
            project={run.project}
            cfg={view.cfg}
            tabs={TAB_ORDER.filter((id) => id !== 'overview').map((id) => ({ id, label: TAB_LABEL[id] }))}
            pinned={pinList}
            metricNames={metricNames}
            onTabHidden={(id, hidden) => view.update((c) => setTabHidden(c, id, hidden))}
            onPinsChange={(pins) => view.update((c) => ({ ...c, pinned: pins }))}
            onShow={onShowPattern}
            onReset={view.reset}
          />
        }
      />

      <div role="tabpanel" id={panelDomId(active)} aria-labelledby={tabDomId(active)} tabIndex={-1} className="outline-none">
        {active === 'overview' && (
          <OverviewTab
            runId={run.run_id}
            running={run.status === 'running'}
            startTime={startTime}
            summary={summary}
            snapshots={snapshots}
            events={runEvents}
            metricsByName={metricsByName}
            pinned={pinnedNames}
            noMetrics={metricNames.length === 0}
            axisSource={axisSource}
            fetchRange={fetchRange}
            liveTick={liveTick}
            showEvents={showEvents}
            isPinned={isPinnedFn}
            onTogglePin={onTogglePin}
            onHide={onHide}
            onSelectRange={onSelectRange}
            onGoTab={onGoTab}
          />
        )}
        {active === 'metrics' && (
          <MetricsTab
            metricNames={metricNames}
            metricsByName={metricsByName}
            events={runEvents}
            startTime={startTime}
            cfg={view.cfg}
            axisSource={axisSource}
            fetchRange={fetchRange}
            liveTick={liveTick}
            showEvents={showEvents}
            onShowEvents={setShowEvents}
            isPinned={isPinnedFn}
            onTogglePin={onTogglePin}
            onHide={onHide}
            onShow={onShowPattern}
            range={range}
            rangeByName={rangeByName}
            rangeZoom={rangeZoom}
          />
        )}
        {active === 'system' && (
          <SystemTab
            run={run}
            summary={summary}
            snapshots={sortedSnapshots}
            gpuIndices={gpuIndices}
            gpuT0={gpuT0}
            gpuSource={gpuSource}
            gpuZoom={gpuExternalZoom}
            hasSamples={sortedSnapshots.length > 0}
          />
        )}
        {active === 'health' && (
          <HealthTab paramStats={paramStats} runId={run.run_id} refreshKey={liveTick} events={runEvents} startTime={startTime} />
        )}
        {active === 'profiler' && (
          <ProfilerTab steps={profileSteps} selected={selectedStep} onSelect={setSelectedStep} events={profileEvents} loading={profileLoading} />
        )}
        {active === 'logs' && <LogsTab logs={logs} />}
        {active === 'media' && <MediaTab run={run} />}
        {active === 'artifacts' && <ArtifactsTab artifacts={artifacts} />}
        {active === 'notes' && <NotesTab run={run} isAdmin={isAdmin} onChanged={fetchRun} />}
      </div>
    </div>
  )
}

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client.ts'
import type { Artifact, Metric, ParamStat, ProfileEvent, Run, RunEvent, RunLog, RunSummary, SystemSnapshot } from '../api/types.ts'
import GradientHeatmap from '../components/GradientHeatmap.tsx'
import LogViewer from '../components/LogViewer.tsx'
import MediaGallery from '../components/MediaGallery.tsx'
import SamplesViewer from '../components/SamplesViewer.tsx'
import MemoryDeltaChart from '../components/MemoryDeltaChart.tsx'
import HyperparamTable from '../components/HyperparamTable.tsx'
import KernelTimeline from '../components/KernelTimeline.tsx'
import ChartDefaults from '../components/ChartDefaults.tsx'
import MetricChart from '../components/MetricChart.tsx'
import CompareChart from '../components/CompareChart.tsx'
import type { RunSeries } from '../components/CompareChart.tsx'
import OperatorBreakdown from '../components/OperatorBreakdown.tsx'
import StatusBadge from '../components/StatusBadge.tsx'
import { RunActions, RunNotes, RunTitle } from '../components/RunOrganizer.tsx'
import { useSession } from '../hooks/useSession.ts'
import { duration, formatBytes, formatMicroseconds, timeAgo } from '../utils/format.ts'
import { useWebSocket } from '../hooks/useWebSocket.ts'
import { timeWindow } from '../utils/xAxis.ts'
import type { XSource } from '../utils/xAxis.ts'
import { backfillTargets, budgetGrew, mergeBuckets, needsResample, tailStart } from '../utils/series.ts'
import { chartPointsBudget, onChartWidthChange } from '../hooks/useMeasuredWidth.ts'
import type { RangeQuery, XZoom } from '../utils/series.ts'
import type { ExternalZoom } from '../components/MetricChart.tsx'

/** Series budget: about two points per pixel of the widest chart on screen (measured with a ResizeObserver);
 * before any chart is mounted, an estimate from the page width. */
function pagePoints(): number {
  return chartPointsBudget(Math.min(Math.max(window.innerWidth - 320, 320), 1000))
}

/** GPU snapshots have no steps: x is elapsed time since the first snapshot, or wall-clock time. */
const GPU_AXES = ['rel', 'time'] as const

const metricKey = (m: Metric) => `${m.name}\u0000${m.step}`
const metricOrder = (a: Metric, b: Metric) => (a.name < b.name ? -1 : a.name > b.name ? 1 : a.step - b.step)
const snapKey = (s: SystemSnapshot) => `${s.gpu_index ?? 0}\u0000${s.timestamp}`
const snapOrder = (a: SystemSnapshot, b: SystemSnapshot) => a.timestamp - b.timestamp || (a.gpu_index ?? 0) - (b.gpu_index ?? 0)
const statKey = (p: ParamStat) => `${p.param_name}\u0000${p.step}`
const statOrder = (a: ParamStat, b: ParamStat) => a.timestamp - b.timestamp || (a.param_name < b.param_name ? -1 : 1)

const EFFICIENCY_COLOR: Record<string, string> = {
  A: '#22c55e',
  B: '#60a5fa',
  C: '#eab308',
  D: '#f97316',
  F: '#ef4444',
}

const GPU_COLORS = ['#22c55e', '#60a5fa', '#f97316', '#a78bfa', '#eab308', '#ec4899', '#14b8a6', '#ef4444']
// Muted variants for whole-device series (same hue as the matching process series)
const GPU_COLORS_DEVICE = ['#2f6b46', '#3a5f8f', '#8a4f25', '#5d4f8f', '#8a7a20', '#8a3a68', '#1f7a70', '#8a3030']

// System metrics shown as charts.
// preferKey: use this column when non-null (per-process attributed value);
//            fall back to key (device-wide) for older runs.
const SYSTEM_SERIES: Array<{
  key: keyof SystemSnapshot
  preferKey?: keyof SystemSnapshot
  label: string
  color: string
  transform?: (v: number) => number
}> = [
  { key: 'gpu_utilization', preferKey: 'attributed_utilization', label: 'GPU Util (%)', color: '#22c55e' },
  { key: 'temperature', label: 'Temperature (°C)', color: '#f97316' },
  { key: 'power_draw', preferKey: 'attributed_power', label: 'Power (W)', color: '#a78bfa' },
  {
    key: 'memory_used',
    preferKey: 'process_memory_used',
    label: 'VRAM (GB)',
    color: '#60a5fa',
    transform: (v) => +(v / 1024 ** 3).toFixed(2),
  },
]

function MetaItem({ label, value, title }: { label: string; value: string | null | undefined; title?: string }) {
  if (!value) return null
  return (
    <div>
      <div className="text-xs mb-0.5" style={{ color: '#4a4e60' }}>
        {label}
      </div>
      <div className="text-xs font-mono leading-relaxed" style={{ color: '#e8eaf0', whiteSpace: 'pre-line' }} title={title}>
        {value}
      </div>
    </div>
  )
}

function Skeleton({ h = 16, w = '100%' }: { h?: number; w?: string | number }) {
  return (
    <div
      className="rounded animate-pulse"
      style={{ height: h, width: w, backgroundColor: '#1a1d24', flexShrink: 0 }}
    />
  )
}

function StatChip({ label, value, highlight = false }: { label: string; value: string; highlight?: boolean }) {
  return (
    <span
      className="text-xs font-mono px-2 py-1 rounded"
      style={{
        backgroundColor: '#111318',
        color: highlight ? '#f97316' : '#8b8fa8',
        border: `1px solid ${highlight ? '#f9731644' : '#252830'}`,
      }}
    >
      <span style={{ color: highlight ? '#f97316' : '#4a4e60' }}>{label} </span>
      {value}
    </span>
  )
}

function RunDetailSkeleton() {
  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-start justify-between mb-5">
        <div className="flex flex-col gap-2">
          <Skeleton h={22} w={220} />
          <Skeleton h={14} w={140} />
        </div>
        <Skeleton h={24} w={110} />
      </div>
      <div
        className="grid gap-x-6 gap-y-3 p-4 rounded-lg mb-6"
        style={{
          backgroundColor: '#111318',
          border: '1px solid #252830',
          gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))',
        }}
      >
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="flex flex-col gap-1.5">
            <Skeleton h={10} w={60} />
            <Skeleton h={14} w="90%" />
          </div>
        ))}
      </div>
      <div
        className="grid gap-4 mb-6"
        style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))' }}
      >
        <Skeleton h={220} />
        <Skeleton h={220} />
      </div>
    </div>
  )
}

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

function OptimizationHints({ events }: { events: ProfileEvent[] }) {
  if (events.length === 0) return null

  const totalUs = events.reduce((acc, e) => acc + e.duration_us, 0)
  const totalCudaUs = events.reduce((acc, e) => acc + e.cuda_time_us, 0)
  const totalCpuUs = events.reduce((acc, e) => acc + e.cpu_time_us, 0)

  const hints: string[] = []

  // Excessive host→device data transfer
  const transferUs = events
    .filter((e) => e.name.includes('aten::to') || e.name.includes('copy_'))
    .reduce((acc, e) => acc + e.duration_us, 0)
  if (totalUs > 0 && transferUs / totalUs > 0.05) {
    hints.push(
      `${((transferUs / totalUs) * 100).toFixed(0)}% of step time in data transfers (aten::to / copy_). ` +
      `Consider pin_memory=True in DataLoader to speed up host→device transfers.`,
    )
  }

  // CPU-bound step
  if (totalCudaUs > 0 && totalCpuUs / totalCudaUs > 2.0) {
    hints.push(
      `CPU time (${formatMicroseconds(totalCpuUs)}) is >2× GPU time (${formatMicroseconds(totalCudaUs)}). ` +
      `GPU may be underutilized — consider torch.compile(), larger batch size, or more DataLoader workers.`,
    )
  }

  // Many small allocations
  const smallAllocCount = events.filter(
    (e) => e.memory_bytes != null && e.memory_bytes > 0 && e.memory_bytes < 1024 * 1024,
  ).length
  if (smallAllocCount > 10) {
    hints.push(
      `${smallAllocCount} operators making allocations <1 MB. Pre-allocating output buffers or wrapping ` +
      `inference with torch.no_grad() can reduce activation memory overhead.`,
    )
  }

  if (hints.length === 0) return null

  return (
    <div className="rounded-lg p-4" style={{ backgroundColor: '#111318', border: '1px solid #252830' }}>
      <div className="text-xs font-mono mb-3" style={{ color: '#8b8fa8' }}>
        Optimization Hints
      </div>
      <div className="flex flex-col gap-2">
        {hints.map((hint, i) => (
          <div key={i} className="flex gap-2">
            <span style={{ color: '#f59e0b', flexShrink: 0 }}>▸</span>
            <span className="text-xs leading-relaxed" style={{ color: '#8b8fa8' }}>
              {hint}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function EventList({ events }: { events: RunEvent[] }) {
  if (events.length === 0) {
    return (
      <div className="px-3 py-2 text-xs font-mono" style={{ color: '#4a4e60' }}>
        None
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-1.5 px-3 pb-3">
      {events.map((ev, i) => {
        const severityColor =
          ev.severity === 'error' ? '#ef4444' : ev.severity === 'warning' ? '#f59e0b' : '#60a5fa'
        return (
          <div key={i} className="flex items-start gap-2 mt-1.5">
            <span style={{ color: severityColor, flexShrink: 0, fontSize: 10, marginTop: 1 }}>▸</span>
            <span className="text-xs font-mono leading-relaxed" style={{ color: '#8b8fa8' }}>
              {ev.step != null && <span style={{ color: '#4a4e60' }}>step {ev.step} · </span>}
              <span style={{ color: '#4a4e60', marginRight: 6 }}>[{ev.event_type}]</span>
              {ev.message}
            </span>
          </div>
        )
      })}
    </div>
  )
}

function CollapsiblePanel({
  label,
  count,
  open,
  onToggle,
  children,
}: {
  label: string
  count?: number
  open: boolean
  onToggle: () => void
  children: React.ReactNode
}) {
  return (
    <div
      className="mt-3 rounded-lg overflow-hidden"
      style={{ backgroundColor: '#111318', border: '1px solid #252830' }}
    >
      <button
        onClick={onToggle}
        className="flex items-center justify-between w-full px-3 py-2 text-left"
        style={{ background: 'none', border: 'none', cursor: 'pointer' }}
      >
        <span className="text-xs font-mono" style={{ color: '#8b8fa8' }}>
          {label}
          {count != null && count > 0 && (
            <span style={{ color: '#4a4e60', marginLeft: 8 }}>{count}</span>
          )}
        </span>
        <span style={{ color: '#4a4e60', fontSize: 9 }}>{open ? '▼' : '▶'}</span>
      </button>
      {open && <div style={{ borderTop: '1px solid #1a1d24' }}>{children}</div>}
    </div>
  )
}

function ModelHealthSection({
  paramStats,
  runId,
  refreshKey,
  runEvents,
  heatmapOpen,
  setHeatmapOpen,
  anomalyOpen,
  setAnomalyOpen,
}: {
  paramStats: ParamStat[]
  runId?: string
  refreshKey?: number
  runEvents: RunEvent[]
  heatmapOpen: boolean
  setHeatmapOpen: React.Dispatch<React.SetStateAction<boolean>>
  anomalyOpen: boolean
  setAnomalyOpen: React.Dispatch<React.SetStateAction<boolean>>
}) {
  const [eventsOpen, setEventsOpen] = useState(false)
  const anomalyEvents = runEvents.filter((ev) => ev.event_type === 'anomaly_detected')

  return (
    <div className="mb-6">
      <button
        onClick={() => setHeatmapOpen((o) => !o)}
        className="flex items-center gap-2 mb-3 w-full text-left"
        style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer' }}
      >
        <h2
          className="text-xs font-mono uppercase"
          style={{ color: '#c8cad6', letterSpacing: '0.12em' }}
        >
          Model Health
        </h2>
        <span style={{ color: '#4a4e60', fontSize: 9 }}>{heatmapOpen ? '▼' : '▶'}</span>
      </button>

      {heatmapOpen && (
        <>
          <GradientHeatmap stats={paramStats} runId={runId} refreshKey={refreshKey} />

          <CollapsiblePanel
            label="Events"
            count={runEvents.length}
            open={eventsOpen}
            onToggle={() => setEventsOpen((o) => !o)}
          >
            <EventList events={runEvents} />
          </CollapsiblePanel>

          <CollapsiblePanel
            label="Anomaly Events"
            count={anomalyEvents.length}
            open={anomalyOpen}
            onToggle={() => setAnomalyOpen((o) => !o)}
          >
            <EventList events={anomalyEvents} />
          </CollapsiblePanel>
        </>
      )}
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
  const [heatmapOpen, setHeatmapOpen] = useState(false)
  const [anomalyOpen, setAnomalyOpen] = useState(false)

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

  if (deleted) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-2" role="alert">
        <span className="text-sm font-mono" style={{ color: '#e8eaf0' }}>This run was deleted.</span>
        <button onClick={() => navigate('/')} className="text-xs font-mono underline" style={{ color: '#8b8fa8' }}>Back to runs</button>
      </div>
    )
  }

  if (loading) {
    return <RunDetailSkeleton />
  }

  if (error || !run) {
    return (
      <div className="flex items-center justify-center h-full">
        <span className="text-sm font-mono" style={{ color: '#ef4444' }}>
          {error ?? 'Run not found'}
        </span>
      </div>
    )
  }

  const metricNames = Object.keys(metricsByName).sort()

  const gitLabel = run.git_hash
    ? `${run.git_hash.slice(0, 8)}${run.git_dirty ? ' ·dirty' : ''}`
    : null

  const gpuLabel = run.gpus && run.gpus.length > 0
    ? run.gpus
        .map((g) => {
          const mem = g.memory_total ? ` · ${formatBytes(g.memory_total)}` : ''
          const mig = g.is_mig ? ' · MIG instance' : g.mig_mode === 'enabled' ? ` · MIG (${g.mig_devices.length})` : ''
          const prefix = run.gpus!.length > 1 ? `${g.index}: ` : ''
          return `${prefix}${g.name ?? 'GPU'}${mem}${mig}`
        })
        .join('\n')
    : [run.gpu_model, run.gpu_memory_total ? formatBytes(run.gpu_memory_total) : null].filter(Boolean).join(' · ')
  const migNote = run.gpus?.some((g) => g.is_mig || g.mig_mode === 'enabled')
    ? 'MIG: NVML reports memory per instance but no utilization, temperature or power'
    : undefined

  // GPUs that reported snapshots (CUDA ordinals); X-axis is elapsed seconds from the first snapshot
  const t0 = Number.isFinite(gpuT0) ? gpuT0 : 0

  return (
    <div className="p-6 max-w-5xl mx-auto">
      {/* ── Header ── */}
      <div className="flex items-start justify-between mb-5">
        <div>
          <RunTitle run={run} isAdmin={isAdmin} onChanged={fetchRun} />
          <div className="flex items-center gap-4 mt-2">
            <StatusBadge status={run.status} />
            <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>
              {timeAgo(run.created_at)}
              {run.finished_at && (
                <> · {duration(run.created_at, run.finished_at)}</>
              )}
            </span>
          </div>
        </div>
        <div className="flex flex-col items-end gap-2">
          <RunActions run={run} isAdmin={isAdmin} canPin={canPin} onChanged={fetchRun} />
          {/* Copy LLM Summary button */}
          <button
            onClick={() => {
              api.getLlmSummary(run.run_id)
                .then((text) => navigator.clipboard.writeText(text))
                .catch(() => {})
            }}
            className="text-xs font-mono px-2 py-1 rounded transition-colors"
            style={{
              backgroundColor: '#1a1d24',
              color: '#8b8fa8',
              border: '1px solid #252830',
              cursor: 'pointer',
            }}
            title="Copy plain-text summary to clipboard (for pasting into an LLM)"
          >
            copy for llm
          </button>
          <span
            className="text-xs font-mono px-2 py-1 rounded select-all"
            style={{ backgroundColor: '#1a1d24', color: '#4a4e60', border: '1px solid #252830' }}
            title={run.run_id}
          >
            {run.run_id.slice(0, 13)}…
          </span>
        </div>
      </div>

      {/* ── Run summary stats ── */}
      {summary && (
        <div
          className="flex flex-wrap items-center gap-2 mb-5"
        >
          {summary.efficiency_score && (
            <span
              className="text-xs font-mono px-2 py-1 rounded font-semibold"
              style={{
                backgroundColor: `${EFFICIENCY_COLOR[summary.efficiency_score]}18`,
                color: EFFICIENCY_COLOR[summary.efficiency_score],
                border: `1px solid ${EFFICIENCY_COLOR[summary.efficiency_score]}44`,
              }}
              title={`Efficiency grade based on GPU utilization (${summary.efficiency_details.gpu_utilization != null ? Math.round(summary.efficiency_details.gpu_utilization * 100) : '?'}%) and training stability`}
            >
              {summary.efficiency_score} grade
            </span>
          )}
          {summary.total_steps != null && (
            <StatChip
              label="steps"
              value={summary.total_steps.toLocaleString()}
            />
          )}
          {summary.steps_per_sec != null && (
            <StatChip
              label="steps/sec"
              value={summary.steps_per_sec.toFixed(1)}
            />
          )}
          {summary.avg_gpu_utilization != null && (
            <StatChip
              label="avg GPU"
              value={`${summary.avg_gpu_utilization.toFixed(1)}%`}
            />
          )}
          {summary.peak_process_memory_bytes != null && (
            <StatChip
              label="peak VRAM"
              value={formatBytes(summary.peak_process_memory_bytes)}
            />
          )}
          {(summary.per_gpu?.length ?? 0) > 1 &&
            summary.per_gpu!.map((g) => {
              const peak = g.peak_process_memory_bytes ?? g.peak_memory_bytes
              return (
                <StatChip
                  key={`gpu-${g.gpu_index}`}
                  label={`GPU ${g.gpu_index}`}
                  value={`${g.avg_gpu_utilization != null ? `${g.avg_gpu_utilization.toFixed(0)}%` : 'n/a'} · ${peak != null ? formatBytes(peak) : 'n/a'}`}
                />
              )
            })}
          {summary.anomaly_count > 0 && (
            <StatChip
              label="anomalies"
              value={String(summary.anomaly_count)}
              highlight
            />
          )}
        </div>
      )}

      {/* ── Hardware / environment metadata ── */}
      <div
        className="grid gap-x-6 gap-y-3 p-4 rounded-lg mb-6"
        style={{
          backgroundColor: '#111318',
          border: '1px solid #252830',
          gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))',
        }}
      >
        <MetaItem label={run.gpus && run.gpus.length > 1 ? `GPUs (${run.gpus.length})` : 'GPU'} value={gpuLabel || null} title={migNote} />
        <MetaItem label="Driver" value={run.driver_version ?? run.gpus?.[0]?.driver_version} />
        <MetaItem label="CUDA" value={run.cuda_version} />
        <MetaItem label="PyTorch" value={run.pytorch_version} />
        <MetaItem label="Python" value={run.python_version} />
        <MetaItem label="Host" value={run.hostname} />
        <MetaItem label="Git" value={gitLabel} />
      </div>

      {/* ── Notes ── */}
      <RunNotes run={run} isAdmin={isAdmin} onChanged={fetchRun} />

      {/* ── Hyperparameters ── */}
      {Object.keys(run.hyperparams).length > 0 && (
        <div className="mb-6">
          <SectionLabel>Hyperparameters</SectionLabel>
          <HyperparamTable hyperparams={run.hyperparams} />
        </div>
      )}

      {/* ── Training metrics ── */}
      {metricNames.length > 0 && (
        <div className="mb-6">
          <div className="flex items-center justify-between mb-3">
            <h2
              className="text-xs font-mono uppercase"
              style={{ color: '#c8cad6', letterSpacing: '0.12em' }}
            >
              Metrics
            </h2>
            <ChartDefaults axisMetrics={metricNames} />
            <button
              onClick={() => setShowEvents((v) => !v)}
              className="text-xs font-mono px-2 py-0.5 rounded"
              style={{
                background: 'none',
                border: '1px solid #252830',
                color: showEvents ? '#22c55e' : '#4a4e60',
                cursor: 'pointer',
              }}
            >
              events {showEvents ? 'on' : 'off'}
            </button>
          </div>
          <div
            className="grid gap-4"
            style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))' }}
          >
            {metricNames.map((name) => (
              <MetricChart
                key={name}
                name={name}
                data={metricsByName[name]}
                events={showEvents ? runEvents : []}
                source={axisSource}
                fetchRange={fetchRange}
                refreshKey={liveTick}
              />
            ))}
          </div>
        </div>
      )}

      {/* ── GPU system stats ── */}
      {sortedSnapshots.length > 0 && (
        <div className="mb-6">
          <SectionLabel>GPU Stats</SectionLabel>
          <div
            className="grid gap-4"
            style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))' }}
          >
            {SYSTEM_SERIES.map(({ key, preferKey, label, color, transform }) => {
              const conv = (v: number) => (transform ? transform(v) : v)
              const toMetrics = (gpu: number, k: keyof SystemSnapshot, name: string): Metric[] =>
                sortedSnapshots
                  .filter((s) => s.gpu_index === gpu && s[k] != null)
                  .map((s) => ({
                    step: Math.round(s.timestamp - t0), // elapsed seconds
                    timestamp: s.timestamp,
                    name,
                    value: conv(s[k] as number),
                  }))

              // One line per GPU; "process" (this run's own usage) and "device" (whole GPU, incl. other
              // processes) are drawn separately when per-process numbers exist.
              const hasProcess = preferKey != null &&
                sortedSnapshots.some((s) => s[preferKey] != null)
              const multi = gpuIndices.length > 1
              const series: RunSeries[] = []
              gpuIndices.forEach((gpu, i) => {
                const base = GPU_COLORS[i % GPU_COLORS.length]
                const tag = multi ? `GPU ${gpu}` : ''
                if (hasProcess && preferKey) {
                  const n = multi ? `${tag} process` : 'process'
                  series.push({ runId: `${gpu}-p`, name: n, color: base, metrics: toMetrics(gpu, preferKey, n), source: gpuSource })
                  const d = multi ? `${tag} device` : 'device'
                  series.push({ runId: `${gpu}-d`, name: d, color: GPU_COLORS_DEVICE[i % GPU_COLORS.length], metrics: toMetrics(gpu, key, d), source: gpuSource })
                } else {
                  const n = multi ? tag : label
                  series.push({ runId: `${gpu}-d`, name: n, color: base, metrics: toMetrics(gpu, key, n), source: gpuSource })
                }
              })
              const nonEmpty = series.filter((s) => s.metrics.length > 0)
              if (nonEmpty.length === 0) return null
              const displayLabel = preferKey && !hasProcess ? `${label} (device)` : label
              if (nonEmpty.length === 1) {
                return (
                  <MetricChart key={key} name={displayLabel} data={nonEmpty[0].metrics} lineColor={color} zoom={gpuExternalZoom} source={gpuSource} axes={GPU_AXES} />
                )
              }
              return (
                <CompareChart key={key} metricName={displayLabel} series={nonEmpty} zoom={gpuExternalZoom} axes={GPU_AXES} />
              )
            })}
          </div>
        </div>
      )}

      {/* ── Model Health ── */}
      {paramStats.length > 0 && (
        <ModelHealthSection
          paramStats={paramStats}
          runId={runId}
          refreshKey={liveTick}
          runEvents={runEvents}
          heatmapOpen={heatmapOpen}
          setHeatmapOpen={setHeatmapOpen}
          anomalyOpen={anomalyOpen}
          setAnomalyOpen={setAnomalyOpen}
        />
      )}

      {/* ── Console logs ── */}
      {logs.length > 0 && (
        <div className="mb-6">
          <SectionLabel>Console</SectionLabel>
          <LogViewer logs={logs} />
        </div>
      )}

      {/* ── Artifacts ── */}
      {artifacts.length > 0 && (
        <div className="mb-6">
          <SectionLabel>Artifacts</SectionLabel>
          <div
            className="rounded-lg overflow-hidden"
            style={{ backgroundColor: '#111318', border: '1px solid #252830' }}
          >
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid #1a1d24' }}>
                  {['type', 'path', 'size', 'step'].map((col) => (
                    <th
                      key={col}
                      className="text-left px-3 py-2 text-xs font-mono"
                      style={{ color: '#4a4e60', fontWeight: 400 }}
                    >
                      {col}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {artifacts.map((a, i) => (
                  <tr
                    key={i}
                    style={{ borderBottom: i < artifacts.length - 1 ? '1px solid #1a1d24' : 'none' }}
                  >
                    <td className="px-3 py-1.5 text-xs font-mono" style={{ color: '#f59e0b' }}>
                      {a.artifact_type}
                    </td>
                    <td
                      className="px-3 py-1.5 text-xs font-mono"
                      style={{ color: '#8b8fa8', wordBreak: 'break-all', maxWidth: 320 }}
                      title={a.path}
                    >
                      {a.path}
                    </td>
                    <td className="px-3 py-1.5 text-xs font-mono" style={{ color: '#4a4e60' }}>
                      {a.size_bytes != null ? formatBytes(a.size_bytes) : '—'}
                    </td>
                    <td className="px-3 py-1.5 text-xs font-mono" style={{ color: '#4a4e60' }}>
                      {a.step ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Media ── */}
      <div className="mb-6">
        <h2
          className="text-xs font-mono uppercase mb-3"
          style={{ color: '#c8cad6', letterSpacing: '0.12em' }}
        >
          Media
        </h2>
        <MediaGallery runId={run.run_id} />
      </div>

      {/* ── Samples (renders nothing when the run logged none) ── */}
      <SamplesViewer runs={[{ runId: run.run_id, label: run.name }]} />

      {/* ── Profiler ── */}
      {profileSteps.length > 0 && (
        <div className="mb-6">
          <SectionLabel>Profiler</SectionLabel>

          {/* Step selector */}
          <div className="flex items-center gap-3 mb-4 flex-wrap">
            <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>
              profiled step
            </span>
            <div className="flex gap-1.5 flex-wrap">
              {profileSteps.map((step) => (
                <button
                  key={step}
                  onClick={() => setSelectedStep(step)}
                  className="text-xs font-mono px-2.5 py-0.5 rounded transition-colors"
                  style={{
                    backgroundColor: selectedStep === step ? '#f59e0b' : '#1a1d24',
                    color: selectedStep === step ? '#0a0b0e' : '#8b8fa8',
                    border: `1px solid ${selectedStep === step ? '#f59e0b' : '#252830'}`,
                    cursor: 'pointer',
                  }}
                >
                  {step}
                </button>
              ))}
            </div>
          </div>

          {profileLoading ? (
            <div
              className="flex items-center justify-center text-xs font-mono rounded-lg"
              style={{ height: 120, backgroundColor: '#111318', border: '1px solid #252830', color: '#4a4e60' }}
            >
              Loading…
            </div>
          ) : (
            <div className="flex flex-col gap-4">
              <KernelTimeline events={profileEvents} />
              <OperatorBreakdown events={profileEvents} />
              <MemoryDeltaChart events={profileEvents} />
              <OptimizationHints events={profileEvents} />
            </div>
          )}
        </div>
      )}

      {/* Empty state */}
      {metricNames.length === 0 && sortedSnapshots.length === 0 && (
        <div
          className="text-center py-12 text-sm font-mono"
          style={{ color: '#4a4e60' }}
        >
          No metrics logged yet
          {run.status === 'running' && (
            <span className="status-pulse ml-2" style={{ color: '#22c55e' }}>
              ●
            </span>
          )}
        </div>
      )}
    </div>
  )
}

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
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
import { duration, formatBytes, formatMicroseconds, timeAgo } from '../utils/format.ts'
import { useWebSocket } from '../hooks/useWebSocket.ts'

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

function MetaItem({ label, value }: { label: string; value: string | null | undefined }) {
  if (!value) return null
  return (
    <div>
      <div className="text-xs mb-0.5" style={{ color: '#4a4e60' }}>
        {label}
      </div>
      <div className="text-xs font-mono leading-relaxed" style={{ color: '#e8eaf0' }}>
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
  runEvents,
  heatmapOpen,
  setHeatmapOpen,
  anomalyOpen,
  setAnomalyOpen,
}: {
  paramStats: ParamStat[]
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
          <GradientHeatmap stats={paramStats} />

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

  const fetchMetrics = useCallback(async () => {
    if (!runId || !mountedRef.current) return
    const data = await api.getMetrics(runId)
    if (!mountedRef.current) return
    setMetrics(data)
  }, [runId])

  const fetchSystem = useCallback(async () => {
    if (!runId || !mountedRef.current) return
    const data = await api.getSystemSnapshots(runId)
    if (!mountedRef.current) return
    setSnapshots(data)
  }, [runId])

  const fetchParamStats = useCallback(async () => {
    if (!runId || !mountedRef.current) return
    const data = await api.getParamStats(runId)
    if (!mountedRef.current) return
    setParamStats(data)
  }, [runId])

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
        const [runData, metricsData, snapshotsData, paramStatsData, eventsData, logsData, artifactsData, summaryData] = await Promise.all([
          api.getRun(runId!),
          api.getMetrics(runId!),
          api.getSystemSnapshots(runId!),
          api.getParamStats(runId!),
          api.getEvents(runId!),
          api.getLogs(runId!),
          api.getArtifacts(runId!),
          api.getRunSummary(runId!).catch(() => null),
        ])
        if (!mountedRef.current) return
        runRef.current = runData
        setRun(runData)
        setMetrics(metricsData)
        setSnapshots(snapshotsData)
        setParamStats(paramStatsData)
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
    initialLoad()

    return () => {
      mountedRef.current = false
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }
  }, [runId, fetchRun, fetchMetrics, fetchSystem, fetchParamStats, fetchEvents, fetchLogs, fetchArtifacts])

  // React immediately to server push events — each event type only refetches its own data
  useWebSocket(
    useCallback(
      (msg) => {
        if (msg.run_id !== runId) return
        if (msg.type === 'metrics') fetchMetrics()
        else if (msg.type === 'system') fetchSystem()
        else if (msg.type === 'param_stats') fetchParamStats()
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
          })
        }
      },
      [runId, fetchRun, fetchMetrics, fetchSystem, fetchParamStats, fetchLogs, fetchSummary],
    ),
  )

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
    () => [...snapshots].sort((a, b) => a.timestamp - b.timestamp),
    [snapshots],
  )
  const gpuIndices = useMemo(
    () => [...new Set(sortedSnapshots.map((s) => s.gpu_index ?? 0))].sort((a, b) => a - b),
    [sortedSnapshots],
  )

  const axisSource = useMemo(
    () => ({
      byName: metricsByName,
      startTime: run ? Date.parse(run.created_at) / 1000 : undefined,
    }),
    [metricsByName, run],
  )

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

  const gpuLabel = [
    run.gpu_model,
    run.gpu_memory_total ? formatBytes(run.gpu_memory_total) : null,
  ]
    .filter(Boolean)
    .join(' · ')

  // GPUs that reported snapshots (CUDA ordinals); X-axis is elapsed seconds from the first snapshot
  const t0 = sortedSnapshots[0]?.timestamp ?? 0

  return (
    <div className="p-6 max-w-5xl mx-auto">
      {/* ── Header ── */}
      <div className="flex items-start justify-between mb-5">
        <div>
          <h1 className="text-lg font-semibold mb-1.5" style={{ color: '#e8eaf0' }}>
            {run.name}
          </h1>
          <div className="flex items-center gap-4">
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

      {/* ── Tags ── */}
      {run.tags.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-5">
          {run.tags.map((tag) => (
            <span
              key={tag}
              className="text-xs font-mono px-2 py-0.5 rounded"
              style={{
                backgroundColor: '#1a1d24',
                color: '#8b8fa8',
                border: '1px solid #252830',
              }}
            >
              #{tag}
            </span>
          ))}
        </div>
      )}

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
        <MetaItem label="GPU" value={gpuLabel || null} />
        <MetaItem label="CUDA" value={run.cuda_version} />
        <MetaItem label="PyTorch" value={run.pytorch_version} />
        <MetaItem label="Python" value={run.python_version} />
        <MetaItem label="Host" value={run.hostname} />
        <MetaItem label="Git" value={gitLabel} />
      </div>

      {/* ── Notes ── */}
      {run.notes && (
        <div
          className="p-4 rounded-lg mb-6 text-sm leading-relaxed"
          style={{
            backgroundColor: '#111318',
            border: '1px solid #252830',
            color: '#8b8fa8',
          }}
        >
          {run.notes}
        </div>
      )}

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
                  series.push({ runId: `${gpu}-p`, name: n, color: base, metrics: toMetrics(gpu, preferKey, n) })
                  const d = multi ? `${tag} device` : 'device'
                  series.push({ runId: `${gpu}-d`, name: d, color: GPU_COLORS_DEVICE[i % GPU_COLORS.length], metrics: toMetrics(gpu, key, d) })
                } else {
                  const n = multi ? tag : label
                  series.push({ runId: `${gpu}-d`, name: n, color: base, metrics: toMetrics(gpu, key, n) })
                }
              })
              const nonEmpty = series.filter((s) => s.metrics.length > 0)
              if (nonEmpty.length === 0) return null
              const displayLabel = preferKey && !hasProcess ? `${label} (device)` : label
              if (nonEmpty.length === 1) {
                return (
                  <MetricChart key={key} name={displayLabel} data={nonEmpty[0].metrics} lineColor={color} />
                )
              }
              return (
                <CompareChart key={key} metricName={displayLabel} series={nonEmpty} />
              )
            })}
          </div>
        </div>
      )}

      {/* ── Model Health ── */}
      {paramStats.length > 0 && (
        <ModelHealthSection
          paramStats={paramStats}
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

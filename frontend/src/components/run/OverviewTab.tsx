import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import type { Metric, RunEvent, RunSummary, SystemSnapshot } from '../../api/types.ts'
import { formatBytes, formatMetricValue } from '../../utils/format.ts'
import type { XSource } from '../../utils/xAxis.ts'
import type { RangeQuery } from '../../utils/series.ts'
import FiringCurve from '../FiringCurve.tsx'
import MetricChart from '../MetricChart.tsx'
import { CHART_GRID, ChartCell, Heading, Note } from './shared.tsx'
import type { TabId } from './shared.tsx'
import { EventRows } from './HealthTab.tsx'

const LOSS_NAMES = ['auto/loss', 'loss', 'train/loss']
const TREND_POINTS = 20

const GRADE_CLASS: Record<string, string> = {
  A: 'text-py-green',
  B: 'text-py-steel',
  C: 'text-py-heat-4',
  D: 'text-py-heat-3',
  F: 'text-py-red',
}

function Cell({ label, value, sub, valueClass = 'text-py-text', onClick, title }: {
  label: string
  value: ReactNode
  sub?: ReactNode
  valueClass?: string
  onClick?: () => void
  title?: string
}) {
  const body = (
    <>
      <div className="log-label">{label}</div>
      <div className={`num mt-1 text-[20px] leading-6 ${valueClass}`}>{value}</div>
      {sub != null && <div className="num mt-0.5 text-[11px] text-py-muted">{sub}</div>}
    </>
  )
  const cls = 'flex min-w-0 flex-col items-start justify-start bg-py-surface px-4 py-2.5 text-left last:odd:col-span-2 sm:min-w-[132px] sm:flex-1 sm:last:odd:col-span-1'
  return onClick ? (
    <button type="button" onClick={onClick} title={title} className={`${cls} transition-colors hover:bg-py-elevated`}>{body}</button>
  ) : (
    <div className={cls} title={title}>{body}</div>
  )
}

/** Latest loss and whether it fell or rose over the last TREND_POINTS logged points. */
function lossReadout(byName: Record<string, Metric[]>): { name: string; value: number; sub: string; trend: 'down' | 'up' | 'flat' } | null {
  const name = LOSS_NAMES.find((n) => (byName[n]?.length ?? 0) > 0)
  if (!name) return null
  const arr = byName[name]
  const last = arr[arr.length - 1]
  const ref = arr[Math.max(0, arr.length - 1 - TREND_POINTS)]
  if (arr.length < 3 || !Number.isFinite(ref.value) || ref.value === 0) return { name, value: last.value, sub: name, trend: 'flat' }
  const pct = ((last.value - ref.value) / Math.abs(ref.value)) * 100
  const trend = Math.abs(pct) < 0.5 ? 'flat' : pct < 0 ? 'down' : 'up'
  const n = arr.length - 1 - Math.max(0, arr.length - 1 - TREND_POINTS)
  const word = trend === 'flat' ? 'flat' : trend === 'down' ? 'falling' : 'rising'
  return { name, value: last.value, trend, sub: `${word} · last ${n} points` }
}

interface Props {
  runId: string
  running: boolean
  startTime: number
  summary: RunSummary | null
  snapshots: SystemSnapshot[]
  events: RunEvent[]
  metricsByName: Record<string, Metric[]>
  pinned: string[]
  noMetrics: boolean
  axisSource: XSource
  fetchRange: (name: string, q: RangeQuery, maxPoints: number) => Promise<Metric[]>
  liveTick: number
  showEvents: boolean
  isPinned: (name: string) => boolean
  onTogglePin: (name: string) => void
  onHide: (name: string) => void
  onSelectRange: (tMin: number, tMax: number) => void
  onGoTab: (tab: TabId) => void
}

export default function OverviewTab(p: Props) {
  const [allEvents, setAllEvents] = useState(false)

  const stepAt = useMemo(() => {
    // The metric with most points gives timestamp -> step; the readout takes the latest step at or before t.
    let best: Metric[] = []
    for (const arr of Object.values(p.metricsByName)) if (arr.length > best.length) best = arr
    const pts = [...best].sort((a, b) => a.timestamp - b.timestamp)
    if (pts.length === 0) return undefined
    return (t: number): number | null => {
      if (t < pts[0].timestamp - 60) return null
      let lo = 0
      let hi = pts.length - 1
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1
        if (pts[mid].timestamp <= t) lo = mid
        else hi = mid - 1
      }
      return pts[lo].step
    }
  }, [p.metricsByName])

  const loss = useMemo(() => lossReadout(p.metricsByName), [p.metricsByName])
  const lastStep = useMemo(() => {
    let m = -1
    for (const arr of Object.values(p.metricsByName)) if (arr.length) m = Math.max(m, arr[arr.length - 1].step)
    return m >= 0 ? m : null
  }, [p.metricsByName])

  const sMean = useMemo(() => {
    const u = p.snapshots.map((s) => s.attributed_utilization ?? s.gpu_utilization).filter((v): v is number => v != null)
    return u.length ? u.reduce((a, b) => a + b, 0) / u.length : null
  }, [p.snapshots])
  const sPeak = useMemo(() => {
    const m = p.snapshots.map((s) => s.process_memory_used ?? s.memory_used).filter((v): v is number => v != null)
    return m.length ? Math.max(...m) : null
  }, [p.snapshots])

  const s = p.summary
  const steps = s?.total_steps ?? lastStep
  const avgGpu = s?.avg_gpu_utilization ?? sMean
  const peakVram = s?.peak_process_memory_bytes ?? sPeak
  const anomalyEvents = p.events.filter((e) => e.event_type === 'anomaly_detected').length
  const anomalies = s ? s.anomaly_count : anomalyEvents > 0 ? anomalyEvents : null
  const grade = s?.efficiency_score ?? null

  const recent = useMemo(() => [...p.events].sort((a, b) => b.timestamp - a.timestamp), [p.events])
  const shown = allEvents ? recent : recent.slice(0, 5)

  const hasCells = loss || steps != null || avgGpu != null || peakVram != null || grade || anomalies != null

  return (
    <div className="flex flex-col gap-5">
      <FiringCurve
        snapshots={p.snapshots}
        events={p.events}
        startTime={p.startTime}
        running={p.running}
        stepAt={stepAt}
        onSelectRange={p.onSelectRange}
      />

      {hasCells && (
        <section aria-label="Key numbers" className="grid grid-cols-2 gap-px overflow-hidden rounded border border-py-border bg-py-border sm:flex sm:overflow-x-auto">
          {loss && (
            <Cell
              label="Loss"
              value={formatMetricValue(loss.value)}
              sub={loss.sub}
              valueClass={loss.trend === 'down' ? 'text-py-green' : 'text-py-text'}
              title={loss.name}
            />
          )}
          {steps != null && (
            <Cell label="Steps" value={steps.toLocaleString()} sub={s?.steps_per_sec != null ? `${s.steps_per_sec.toFixed(1)} steps/s` : undefined} />
          )}
          {avgGpu != null && <Cell label="Avg GPU" value={`${avgGpu.toFixed(1)}%`} sub={p.running ? 'so far' : 'whole run'} />}
          {peakVram != null && <Cell label="Peak VRAM" value={formatBytes(peakVram)} />}
          {grade && (
            <Cell
              label="Efficiency"
              value={grade}
              valueClass={GRADE_CLASS[grade] ?? 'text-py-text'}
              sub={s?.efficiency_details?.gpu_utilization != null ? 'GPU util + stability' : 'grade'}
              title="Efficiency grade from GPU utilisation and training stability"
            />
          )}
          {anomalies != null && (
            <Cell
              label="Anomalies"
              value={anomalies}
              valueClass={anomalies > 0 ? 'text-py-red' : 'text-py-text'}
              sub={anomalies > 0 ? 'open Model health' : 'none detected'}
              onClick={anomalies > 0 ? () => p.onGoTab('health') : undefined}
              title={anomalies > 0 ? 'Show anomalies in Model health' : undefined}
            />
          )}
        </section>
      )}

      {p.noMetrics ? (
        <Note>
          <div className="mb-1 text-py-soft">No metrics logged yet.</div>
          Metrics appear here as the run logs them. With autolog on (<span className="num">with pyrometer.init(...) as run:</span>)
          loss, learning rate and throughput are recorded for you; log your own with <span className="num">run.log({'{'}"val/acc": acc{'}'})</span>.
        </Note>
      ) : (
        <section aria-label="Pinned charts">
          <Heading>Pinned charts</Heading>
          {p.pinned.length === 0 ? (
            <Note>
              Nothing is pinned. Pin a chart from the{' '}
              <button type="button" className="text-py-soft underline hover:text-py-text" onClick={() => p.onGoTab('metrics')}>Metrics tab</button>{' '}
              to keep it here for every run in this project.
            </Note>
          ) : (
            <div className={CHART_GRID}>
              {p.pinned.map((name) => (
                <ChartCell key={name} name={name} pinned={p.isPinned(name)} onTogglePin={() => p.onTogglePin(name)} onHide={() => p.onHide(name)}>
                  <MetricChart
                    name={name}
                    data={p.metricsByName[name]}
                    events={p.showEvents ? p.events : []}
                    source={p.axisSource}
                    fetchRange={p.fetchRange}
                    refreshKey={p.liveTick}
                  />
                </ChartCell>
              ))}
            </div>
          )}
        </section>
      )}

      {recent.length > 0 && (
        <section aria-label="Recent events">
          <Heading
            right={
              recent.length > 5 && (
                <button type="button" className="text-xs text-py-soft underline hover:text-py-text" onClick={() => setAllEvents((v) => !v)}>
                  {allEvents ? 'Show fewer' : `View all ${recent.length}`}
                </button>
              )
            }
          >
            Recent events
          </Heading>
          <div className={`rounded border border-py-border bg-py-surface ${allEvents ? 'max-h-96 overflow-y-auto' : ''}`}>
            <EventRows events={shown} startTime={p.startTime} />
          </div>
        </section>
      )}
    </div>
  )
}

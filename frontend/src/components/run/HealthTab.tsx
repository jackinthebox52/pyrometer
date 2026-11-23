import type { ParamStat, RunEvent } from '../../api/types.ts'
import GradientHeatmap from '../GradientHeatmap.tsx'
import { Heading, Note } from './shared.tsx'

const SEV_DOT: Record<string, string> = {
  error: 'bg-py-red',
  warning: 'bg-py-heat-4',
}

function fmtOffset(secs: number): string {
  const s = Math.max(0, Math.round(secs))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}h ${String(m).padStart(2, '0')}m` : m > 0 ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`
}

/** Event rows: severity square, elapsed time and step (mono), type (mono), message (prose). */
export function EventRows({ events, startTime }: { events: RunEvent[]; startTime: number }) {
  if (events.length === 0) return <div className="px-3 py-2 text-[13px] text-py-muted">None.</div>
  return (
    <ul className="divide-y divide-py-border">
      {events.map((ev, i) => (
        <li key={i} className="flex items-start gap-3 px-3 py-2 text-[13px]">
          <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-[1px] ${SEV_DOT[ev.severity] ?? 'bg-py-faint'}`} aria-label={ev.severity} />
          <span className="num w-[72px] shrink-0 text-xs leading-5 text-py-muted">{fmtOffset(ev.timestamp - startTime)}</span>
          <span className="num shrink-0 text-xs leading-5 text-py-muted">{ev.step != null ? `step ${ev.step}` : ''}</span>
          <span className="min-w-0 text-py-soft">
            <span className="num mr-2 text-xs text-py-muted">{ev.event_type}</span>
            {ev.message}
          </span>
        </li>
      ))}
    </ul>
  )
}

interface Props {
  paramStats: ParamStat[]
  runId: string
  refreshKey: number
  events: RunEvent[]
  startTime: number
}

export default function HealthTab({ paramStats, runId, refreshKey, events, startTime }: Props) {
  const anomalies = events.filter((e) => e.event_type === 'anomaly_detected')
  return (
    <div className="flex flex-col gap-6">
      <section>
        <Heading>Gradient and weight health</Heading>
        {paramStats.length > 0 ? (
          <GradientHeatmap stats={paramStats} runId={runId} refreshKey={refreshKey} />
        ) : (
          <Note>No gradient statistics. Call <span className="num">run.watch(model)</span> to record them.</Note>
        )}
      </section>
      <section>
        <Heading>Anomalies ({anomalies.length})</Heading>
        <div className="rounded border border-py-border bg-py-surface">
          <EventRows events={anomalies} startTime={startTime} />
        </div>
      </section>
      <section>
        <Heading>All events ({events.length})</Heading>
        <div className="max-h-[28rem] overflow-y-auto rounded border border-py-border bg-py-surface">
          <EventRows events={events} startTime={startTime} />
        </div>
      </section>
    </div>
  )
}

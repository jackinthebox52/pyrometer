import { useCallback, useMemo, useRef, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { Metric, RunEvent } from '../api/types.ts'
import { formatMetricValue } from '../utils/format.ts'
import ExpandModal, { ExpandButton } from './ExpandModal.tsx'

interface Props {
  name: string
  data: Metric[]
  lineColor?: string
  /** Events to render as vertical marker lines. */
  events?: RunEvent[]
  /** When true, renders taller (for use inside ExpandModal). */
  expanded?: boolean
}

/** Apply exponential moving average. alpha=0 → no smoothing, alpha→1 → heavy smoothing. */
function applyEMA(data: Metric[], alpha: number): Metric[] {
  if (alpha === 0 || data.length === 0) return data
  const out: Metric[] = []
  let prev = data[0].value
  for (const d of data) {
    const v = alpha * prev + (1 - alpha) * d.value
    out.push({ ...d, value: v })
    prev = v
  }
  return out
}

function eventColor(severity: string): string {
  if (severity === 'error') return '#ef4444'
  if (severity === 'warning') return '#f59e0b'
  return '#60a5fa'
}

function Chart({
  data,
  lineColor,
  height,
  events,
}: {
  data: Metric[]
  lineColor: string
  height: number
  events?: RunEvent[]
}) {
  // Deduplicate events by step (keep first per step to avoid overlapping lines)
  const eventMarkers = useMemo(() => {
    if (!events || events.length === 0) return []
    const seen = new Set<number>()
    return events.filter((e) => {
      if (e.step == null) return false
      if (seen.has(e.step)) return false
      seen.add(e.step)
      return true
    })
  }, [events])

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 4, right: 4, left: 4, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e2128" vertical={false} />
        <XAxis
          dataKey="step"
          tick={{ fontSize: 10, fill: '#4a4e60', fontFamily: 'JetBrains Mono' }}
          tickLine={false}
          axisLine={false}
          minTickGap={48}
        />
        <YAxis
          tick={{ fontSize: 10, fill: '#4a4e60', fontFamily: 'JetBrains Mono' }}
          tickLine={false}
          axisLine={false}
          tickFormatter={formatMetricValue}
          width={35}
        />
        <Tooltip
          contentStyle={{
            backgroundColor: '#1a1d24',
            border: '1px solid #353840',
            borderRadius: 6,
            fontSize: 12,
            fontFamily: 'JetBrains Mono',
            padding: '6px 10px',
          }}
          labelStyle={{ color: '#8b8fa8', marginBottom: 2 }}
          itemStyle={{ color: lineColor }}
          labelFormatter={(v) => `step ${v}`}
          formatter={(v: number) => [formatMetricValue(v), '']}
          cursor={{ stroke: '#353840', strokeWidth: 1 }}
        />
        {eventMarkers.map((ev, i) => (
          <ReferenceLine
            key={i}
            x={ev.step!}
            stroke={eventColor(ev.severity)}
            strokeWidth={1}
            strokeDasharray="3 3"
            label={{
              value: '●',
              position: 'top',
              fontSize: 7,
              fill: eventColor(ev.severity),
            }}
          />
        ))}
        <Line
          type="monotone"
          dataKey="value"
          stroke={lineColor}
          strokeWidth={1.5}
          dot={false}
          activeDot={{ r: 3, fill: lineColor, strokeWidth: 0 }}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  )
}

export default function MetricChart({
  name,
  data,
  lineColor = '#f59e0b',
  events,
  expanded = false,
}: Props) {
  const [alpha, setAlpha] = useState(0)
  const [isExpanded, setIsExpanded] = useState(false)
  const [showLocalEvents, setShowLocalEvents] = useState(true)
  const containerRef = useRef<HTMLDivElement>(null)

  const smoothed = useMemo(() => applyEMA(data, alpha), [data, alpha])
  const last = smoothed.length > 0 ? smoothed[smoothed.length - 1].value : null

  const onWheel = useCallback((e: React.WheelEvent) => {
    if (!e.shiftKey) return
    e.preventDefault()
    setAlpha((a) => Math.min(0.97, Math.max(0, a + (e.deltaY < 0 ? 0.05 : -0.05))))
  }, [])

  const chartHeight = expanded ? 440 : 160
  const smoothPct = Math.round(alpha * 100)

  return (
    <>
      <div
        ref={containerRef}
        className="rounded-lg p-4"
        style={{ backgroundColor: '#111318', border: '1px solid #252830' }}
        onWheel={onWheel}
      >
        <div className="flex items-baseline justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className="text-xs font-mono" style={{ color: '#8b8fa8' }}>
              {name}
            </span>
            {alpha > 0 && (
              <span
                className="text-xs font-mono"
                style={{ color: '#4a4e60' }}
                title="Shift+scroll to adjust smoothing"
              >
                smooth {smoothPct}%
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            {last !== null && (
              <span className="text-sm font-mono font-medium" style={{ color: lineColor }}>
                {formatMetricValue(last)}
              </span>
            )}
            {!expanded && events && events.length > 0 && (
              <button
                onClick={() => setShowLocalEvents((v) => !v)}
                title={showLocalEvents ? 'Hide event markers' : 'Show event markers'}
                style={{
                  background: 'none',
                  border: 'none',
                  padding: '0 2px',
                  cursor: 'pointer',
                  fontSize: 10,
                  color: showLocalEvents ? '#60a5fa' : '#2a2d38',
                  lineHeight: 1,
                }}
              >
                ◆
              </button>
            )}
            {!expanded && <ExpandButton onClick={() => setIsExpanded(true)} />}
          </div>
        </div>

        {data.length === 0 ? (
          <div
            className="flex items-center justify-center text-xs font-mono"
            style={{ height: 160, color: '#4a4e60' }}
          >
            no data
          </div>
        ) : (
          <Chart data={smoothed} lineColor={lineColor} height={chartHeight} events={showLocalEvents ? events : []} />
        )}

        {!expanded && (
          <div className="mt-1 text-right">
            <span className="text-xs font-mono" style={{ color: '#2a2d38' }}>
              shift+scroll to smooth
            </span>
          </div>
        )}
      </div>

      {isExpanded && (
        <ExpandModal onClose={() => setIsExpanded(false)}>
          <MetricChart
            name={name}
            data={data}
            lineColor={lineColor}
            events={events}
            expanded
          />
        </ExpandModal>
      )}
    </>
  )
}

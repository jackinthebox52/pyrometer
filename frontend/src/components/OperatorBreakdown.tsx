import { useState } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { ProfileEvent } from '../api/types.ts'
import { formatMicroseconds } from '../utils/format.ts'
import { CHART, HEAT, N, STEEL } from '../theme.ts'
import ExpandModal, { ExpandButton } from './ExpandModal.tsx'

interface Props {
  events: ProfileEvent[]
  expanded?: boolean
}

export default function OperatorBreakdown({ events, expanded = false }: Props) {
  const [isExpanded, setIsExpanded] = useState(false)

  if (events.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-sm border border-py-border bg-py-surface text-xs text-py-faint"
        style={{ height: 160 }}
      >
        No profile data for this step
      </div>
    )
  }

  const totalUs = events.reduce((acc, e) => acc + e.duration_us, 0)

  // Show more rows when expanded
  const limit = expanded ? 40 : 20
  const top = [...events]
    .sort((a, b) => b.duration_us - a.duration_us)
    .slice(0, limit)
    .map((e) => ({
      name: e.name.length > 28 ? `${e.name.slice(0, 27)}…` : e.name,
      fullName: e.name,
      cuda: e.cuda_time_us,
      cpu: e.cpu_time_us,
      total: e.duration_us,
      pct: ((e.duration_us / totalUs) * 100).toFixed(1),
    }))
    .reverse()

  const height = Math.max(200, top.length * 26 + 60)

  return (
    <>
    <div className="rounded-sm border border-py-border bg-py-surface p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[13px] font-semibold text-py-text">Operator breakdown</span>
        <div className="flex items-center gap-3">
          <span className="text-[11px] text-py-muted">
            step total {formatMicroseconds(totalUs)}
          </span>
          {!expanded && <ExpandButton onClick={() => setIsExpanded(true)} />}
        </div>
      </div>

      <div className="flex gap-4 mb-3">
        <div className="flex items-center gap-1.5">
          <div className="h-2 w-2 rounded-[1px]" style={{ backgroundColor: HEAT[4] }} />
          <span className="text-[11px] text-py-muted">CUDA</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="h-2 w-2 rounded-[1px]" style={{ backgroundColor: STEEL }} />
          <span className="text-[11px] text-py-muted">CPU</span>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={top} layout="vertical" margin={{ top: 0, right: 56, left: 16, bottom: 0 }} barSize={13}>
          <CartesianGrid strokeDasharray="2 4" stroke={CHART.grid} horizontal={false} />
          <XAxis
            type="number"
            tick={CHART.tick}
            tickLine={false}
            axisLine={{ stroke: CHART.axis }}
            tickFormatter={formatMicroseconds}
          />
          <YAxis
            type="category"
            dataKey="name"
            width={165}
            tick={{ ...CHART.tick, fill: N.n8 }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip
            {...CHART.tooltip}
            formatter={(value: number, name: string, props) => {
              const label = name === 'cuda' ? 'CUDA' : 'CPU'
              const pct = props.payload.total > 0
                ? ((value / props.payload.total) * 100).toFixed(1)
                : '0.0'
              return [`${formatMicroseconds(value)} (${pct}% of op)`, label]
            }}
            cursor={{ fill: N.n3, fillOpacity: 0.5 }}
          />
          <Bar dataKey="cuda" stackId="a" fill={HEAT[4]} isAnimationActive={false} radius={0} />
          <Bar dataKey="cpu" stackId="a" fill={STEEL} isAnimationActive={false} radius={[0, 2, 2, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>

    {isExpanded && (
      <ExpandModal onClose={() => setIsExpanded(false)}>
        <OperatorBreakdown events={events} expanded />
      </ExpandModal>
    )}
    </>
  )
}

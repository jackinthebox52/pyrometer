import { useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { ProfileEvent } from '../api/types.ts'
import { formatBytesSigned } from '../utils/format.ts'
import { CHART, HEAT, N, STEEL } from '../theme.ts'
import ExpandModal, { ExpandButton } from './ExpandModal.tsx'

interface Props {
  events: ProfileEvent[]
  expanded?: boolean
}

function fmtBytesAbs(b: number): string {
  const abs = Math.abs(b)
  if (abs >= 1024 ** 3) return `${(abs / 1024 ** 3).toFixed(2)}GB`
  if (abs >= 1024 ** 2) return `${(abs / 1024 ** 2).toFixed(1)}MB`
  if (abs >= 1024) return `${(abs / 1024).toFixed(1)}KB`
  return `${abs}B`
}

export default function MemoryDeltaChart({ events, expanded = false }: Props) {
  const [isExpanded, setIsExpanded] = useState(false)
  const limit = expanded ? 30 : 15
  const withMem = events
    .filter((e) => e.memory_bytes != null && e.memory_bytes !== 0)
    .sort((a, b) => Math.abs(b.memory_bytes!) - Math.abs(a.memory_bytes!))
    .slice(0, limit)
    .map((e) => ({
      name: e.name.length > 24 ? `${e.name.slice(0, 23)}…` : e.name,
      bytes: e.memory_bytes!,
    }))
    .reverse()

  if (withMem.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-sm border border-py-border bg-py-surface text-xs text-py-faint"
        style={{ height: 120 }}
      >
        No memory data for this step
      </div>
    )
  }

  const height = Math.max(140, withMem.length * 26 + 60)

  return (
    <>
    <div className="rounded-sm border border-py-border bg-py-surface p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[13px] font-semibold text-py-text">Memory delta by operator</span>
        <div className="flex items-center gap-3">
          <span className="text-[11px] text-py-muted">
            Net VRAM change per op
          </span>
          {!expanded && <ExpandButton onClick={() => setIsExpanded(true)} />}
        </div>
      </div>

      <div className="flex gap-4 mb-3">
        <div className="flex items-center gap-1.5">
          <div className="h-2 w-2 rounded-[1px]" style={{ backgroundColor: HEAT[3] }} />
          <span className="text-[11px] text-py-muted">Alloc</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="h-2 w-2 rounded-[1px]" style={{ backgroundColor: STEEL }} />
          <span className="text-[11px] text-py-muted">Free</span>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={withMem} layout="vertical" margin={{ top: 0, right: 56, left: 16, bottom: 0 }} barSize={13}>
          <CartesianGrid strokeDasharray="2 4" stroke={CHART.grid} horizontal={false} />
          <XAxis
            type="number"
            tick={CHART.tick}
            tickLine={false}
            axisLine={{ stroke: CHART.axis }}
            tickFormatter={fmtBytesAbs}
          />
          <YAxis
            type="category"
            dataKey="name"
            width={140}
            tick={{ ...CHART.tick, fill: N.n8 }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip
            {...CHART.tooltip}
            formatter={(value: number) => [formatBytesSigned(value), 'Memory delta']}
            cursor={{ fill: N.n3, fillOpacity: 0.5 }}
          />
          <Bar dataKey="bytes" isAnimationActive={false} radius={[0, 2, 2, 0]}>
            {withMem.map((entry, i) => (
              <Cell key={i} fill={entry.bytes >= 0 ? HEAT[3] : STEEL} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>

    {isExpanded && (
      <ExpandModal onClose={() => setIsExpanded(false)}>
        <MemoryDeltaChart events={events} expanded />
      </ExpandModal>
    )}
    </>
  )
}

import { useState } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { ProfileEvent } from '../api/types.ts'
import { formatMicroseconds } from '../utils/format.ts'
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
        className="flex items-center justify-center text-xs font-mono rounded-lg"
        style={{ height: 160, backgroundColor: '#111318', border: '1px solid #252830', color: '#4a4e60' }}
      >
        no profile data
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
    <div className="rounded-lg p-4" style={{ backgroundColor: '#111318', border: '1px solid #252830' }}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-mono" style={{ color: '#8b8fa8' }}>
          Operator Breakdown
        </span>
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>
            step total {formatMicroseconds(totalUs)}
          </span>
          {!expanded && <ExpandButton onClick={() => setIsExpanded(true)} />}
        </div>
      </div>

      <div className="flex gap-4 mb-3">
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-sm" style={{ backgroundColor: '#f59e0b' }} />
          <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>CUDA</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-sm" style={{ backgroundColor: '#6366f1' }} />
          <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>CPU</span>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={top} layout="vertical" margin={{ top: 0, right: 56, left: 16, bottom: 0 }} barSize={13}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e2128" horizontal={false} />
          <XAxis
            type="number"
            tick={{ fontSize: 10, fill: '#4a4e60', fontFamily: 'JetBrains Mono' }}
            tickLine={false}
            axisLine={false}
            tickFormatter={formatMicroseconds}
          />
          <YAxis
            type="category"
            dataKey="name"
            width={165}
            tick={{ fontSize: 10, fill: '#8b8fa8', fontFamily: 'JetBrains Mono' }}
            tickLine={false}
            axisLine={false}
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
            labelStyle={{ color: '#e8eaf0', marginBottom: 4 }}
            formatter={(value: number, name: string, props) => {
              const label = name === 'cuda' ? 'CUDA' : 'CPU'
              const pct = props.payload.total > 0
                ? ((value / props.payload.total) * 100).toFixed(1)
                : '0.0'
              return [`${formatMicroseconds(value)} (${pct}% of op)`, label]
            }}
            cursor={{ fill: 'rgba(255,255,255,0.03)' }}
          />
          <Bar dataKey="cuda" stackId="a" fill="#f59e0b" isAnimationActive={false} radius={0} />
          <Bar dataKey="cpu" stackId="a" fill="#6366f1" isAnimationActive={false} radius={[0, 2, 2, 0]} />
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

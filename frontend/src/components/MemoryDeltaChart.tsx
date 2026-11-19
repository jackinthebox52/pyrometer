import { useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { ProfileEvent } from '../api/types.ts'
import { formatBytesSigned } from '../utils/format.ts'
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
        className="flex items-center justify-center text-xs font-mono rounded-lg"
        style={{ height: 120, backgroundColor: '#111318', border: '1px solid #252830', color: '#4a4e60' }}
      >
        no memory data
      </div>
    )
  }

  const height = Math.max(140, withMem.length * 26 + 60)

  return (
    <>
    <div className="rounded-lg p-4" style={{ backgroundColor: '#111318', border: '1px solid #252830' }}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-mono" style={{ color: '#8b8fa8' }}>
          Memory Delta by Operator
        </span>
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>
            net VRAM Δ per op
          </span>
          {!expanded && <ExpandButton onClick={() => setIsExpanded(true)} />}
        </div>
      </div>

      <div className="flex gap-4 mb-3">
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-sm" style={{ backgroundColor: '#22c55e' }} />
          <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>Alloc</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-sm" style={{ backgroundColor: '#ef4444' }} />
          <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>Free</span>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={withMem} layout="vertical" margin={{ top: 0, right: 56, left: 16, bottom: 0 }} barSize={13}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e2128" horizontal={false} />
          <XAxis
            type="number"
            tick={{ fontSize: 10, fill: '#4a4e60', fontFamily: 'JetBrains Mono' }}
            tickLine={false}
            axisLine={false}
            tickFormatter={fmtBytesAbs}
          />
          <YAxis
            type="category"
            dataKey="name"
            width={140}
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
            itemStyle={{ color: '#e8eaf0' }}
            formatter={(value: number) => [formatBytesSigned(value), 'Memory Δ']}
            cursor={{ fill: 'rgba(255,255,255,0.03)' }}
          />
          <Bar dataKey="bytes" isAnimationActive={false} radius={[0, 2, 2, 0]}>
            {withMem.map((entry, i) => (
              <Cell key={i} fill={entry.bytes >= 0 ? '#22c55e' : '#ef4444'} />
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

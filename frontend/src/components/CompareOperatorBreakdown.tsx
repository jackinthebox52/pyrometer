import { useState } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { ProfileEvent } from '../api/types.ts'
import { formatMicroseconds } from '../utils/format.ts'
import ExpandModal, { ExpandButton } from './ExpandModal.tsx'

export interface ProfileSeries {
  runId: string
  name: string
  color: string
  events: ProfileEvent[]
}

interface Props {
  series: ProfileSeries[]
  expanded?: boolean
}

export default function CompareOperatorBreakdown({ series, expanded = false }: Props) {
  const [isExpanded, setIsExpanded] = useState(false)

  const hasData = series.some((s) => s.events.length > 0)

  if (!hasData) {
    return (
      <div
        className="flex items-center justify-center text-xs font-mono rounded-lg"
        style={{ height: 160, backgroundColor: '#111318', border: '1px solid #252830', color: '#4a4e60' }}
      >
        no profile data
      </div>
    )
  }

  const limit = expanded ? 30 : 15

  // Per-run lookup: op name → total duration_us
  const runMaps = series.map((s) => {
    const m = new Map<string, number>()
    for (const e of s.events) m.set(e.name, e.duration_us)
    return m
  })

  // Union of top N ops from each run
  const opNames = new Set<string>()
  for (const s of series) {
    ;[...s.events]
      .sort((a, b) => b.duration_us - a.duration_us)
      .slice(0, limit)
      .forEach((e) => opNames.add(e.name))
  }

  // Build chart rows; sort ascending by max-across-runs so largest ends up at bottom
  const rows = [...opNames]
    .map((name) => {
      const row: Record<string, number | string> = {
        name: name.length > 28 ? `${name.slice(0, 27)}…` : name,
      }
      let maxVal = 0
      series.forEach((s, i) => {
        const v = runMaps[i].get(name) ?? 0
        row[s.runId] = v
        if (v > maxVal) maxVal = v
      })
      row._max = maxVal
      return row
    })
    .sort((a, b) => (a._max as number) - (b._max as number))

  const rowHeight = series.length * 11 + 10
  const height = Math.max(200, rows.length * rowHeight + 60)

  return (
    <>
      <div className="rounded-lg p-4" style={{ backgroundColor: '#111318', border: '1px solid #252830' }}>
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-mono" style={{ color: '#8b8fa8' }}>
            Operator Breakdown
          </span>
          {!expanded && <ExpandButton onClick={() => setIsExpanded(true)} />}
        </div>

        <div className="flex flex-wrap gap-4 mb-3">
          {series.map((s) => (
            <div key={s.runId} className="flex items-center gap-1.5">
              <div className="w-2 h-2 rounded-sm" style={{ backgroundColor: s.color }} />
              <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>{s.name}</span>
            </div>
          ))}
        </div>

        <ResponsiveContainer width="100%" height={height}>
          <BarChart
            data={rows}
            layout="vertical"
            margin={{ top: 0, right: 56, left: 16, bottom: 0 }}
            barSize={9}
          >
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
              formatter={(value: number, runId: string) => {
                const run = series.find((s) => s.runId === runId)
                return [formatMicroseconds(value), run?.name ?? runId]
              }}
              cursor={{ fill: 'rgba(255,255,255,0.03)' }}
            />
            {series.map((s) => (
              <Bar
                key={s.runId}
                dataKey={s.runId}
                fill={s.color}
                isAnimationActive={false}
                radius={[0, 2, 2, 0]}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>

      {isExpanded && (
        <ExpandModal onClose={() => setIsExpanded(false)}>
          <CompareOperatorBreakdown series={series} expanded />
        </ExpandModal>
      )}
    </>
  )
}

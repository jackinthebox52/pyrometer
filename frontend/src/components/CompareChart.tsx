import { useCallback, useEffect, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { Metric } from '../api/types.ts'
import { formatMetricValue } from '../utils/format.ts'
import ExpandModal, { ExpandButton } from './ExpandModal.tsx'

export interface RunSeries {
  runId: string
  name: string
  color: string
  metrics: Metric[]
}

interface Props {
  metricName: string
  series: RunSeries[]
  smoothing: number   // global slider value from CompareView
  globalHiddenIds?: Set<string>
  expanded?: boolean
}

/** Exponential moving average. alpha=0 → raw, alpha→1 → very smooth. */
function applyEMA(values: number[], alpha: number): number[] {
  if (alpha === 0 || values.length === 0) return values
  const result: number[] = []
  let ema = values[0]
  for (const v of values) {
    ema = alpha * ema + (1 - alpha) * v
    result.push(ema)
  }
  return result
}

/** Merge multiple run series into [{step, runId1: value, runId2: value, …}] for Recharts. */
function mergeSeriesData(
  series: RunSeries[],
  alpha: number,
): Record<string, number | string>[] {
  const stepMap = new Map<number, Record<string, number | string>>()
  for (const run of series) {
    const smoothed = applyEMA(run.metrics.map((m) => m.value), alpha)
    run.metrics.forEach((m, i) => {
      if (!stepMap.has(m.step)) stepMap.set(m.step, { step: m.step })
      stepMap.get(m.step)![run.runId] = smoothed[i]
    })
  }
  return [...stepMap.values()].sort((a, b) => (a.step as number) - (b.step as number))
}

export default function CompareChart({
  metricName,
  series,
  smoothing,
  globalHiddenIds,
  expanded = false,
}: Props) {
  const [alpha, setAlpha] = useState(smoothing)
  useEffect(() => { setAlpha(smoothing) }, [smoothing])

  const [isExpanded, setIsExpanded] = useState(false)
  const [localHiddenIds, setLocalHiddenIds] = useState<Set<string>>(new Set())

  const isHidden = (runId: string) =>
    localHiddenIds.has(runId) || (globalHiddenIds?.has(runId) ?? false)

  const toggleLocal = (runId: string) => {
    setLocalHiddenIds((prev) => {
      const next = new Set(prev)
      if (next.has(runId)) next.delete(runId)
      else next.add(runId)
      return next
    })
  }

  const onWheel = useCallback((e: React.WheelEvent) => {
    if (!e.shiftKey) return
    e.preventDefault()
    setAlpha((a) => Math.min(0.97, Math.max(0, a + (e.deltaY < 0 ? 0.05 : -0.05))))
  }, [])

  const visibleSeries = series.filter((s) => !isHidden(s.runId))
  const hasData = visibleSeries.some((s) => s.metrics.length > 0)
  const data = hasData ? mergeSeriesData(visibleSeries, alpha) : []
  const smoothPct = Math.round(alpha * 100)
  const chartHeight = expanded ? 440 : 200

  return (
    <>
      <div
        className="rounded-lg p-4 flex flex-col"
        style={{ backgroundColor: '#111318', border: '1px solid #252830', height: '100%' }}
        onWheel={onWheel}
      >
        {/* Header */}
        <div className="flex items-center justify-between mb-2 gap-2">
          <div className="flex items-center gap-2">
            <span className="text-xs font-mono" style={{ color: '#8b8fa8' }}>
              {metricName}
            </span>
            {alpha > 0 && (
              <span className="text-xs font-mono" style={{ color: '#4a4e60' }}
                title="Shift+scroll to adjust smoothing">
                smooth {smoothPct}%
              </span>
            )}
          </div>
          {!expanded && <ExpandButton onClick={() => setIsExpanded(true)} />}
        </div>
        <div className="flex flex-wrap gap-3 mb-3">
          {series.map((s) => {
            const hidden = isHidden(s.runId)
            const last = !hidden && s.metrics.length > 0 ? s.metrics[s.metrics.length - 1].value : null
            return (
              <span
                key={s.runId}
                className="text-xs font-mono flex items-center gap-1.5 cursor-pointer select-none"
                style={{ opacity: hidden ? 0.35 : 1 }}
                onClick={() => toggleLocal(s.runId)}
                title={hidden ? 'Click to show' : 'Click to hide'}
              >
                <span style={{
                  display: 'inline-block', width: 20, height: 2,
                  backgroundColor: s.color, borderRadius: 1, flexShrink: 0,
                }} />
                <span style={{ color: '#8b8fa8' }}>{s.name}</span>
                {last !== null && (
                  <span style={{ color: s.color }}>{formatMetricValue(last)}</span>
                )}
              </span>
            )
          })}
        </div>

        <div style={{ marginTop: 'auto' }}>
          {!hasData ? (
            <div className="flex items-center justify-center text-xs font-mono"
              style={{ height: chartHeight, color: '#4a4e60' }}>
              no data
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={chartHeight}>
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
                  labelStyle={{ color: '#8b8fa8', marginBottom: 4 }}
                  labelFormatter={(v) => `step ${v}`}
                  formatter={(v: number, runId: string) => {
                    const run = series.find((s) => s.runId === runId)
                    return [formatMetricValue(v), run?.name ?? runId]
                  }}
                  itemStyle={{ padding: '1px 0' }}
                  cursor={{ stroke: '#353840', strokeWidth: 1 }}
                />
                {visibleSeries.map((s) => (
                  <Line
                    key={s.runId}
                    type="monotone"
                    dataKey={s.runId}
                    stroke={s.color}
                    strokeWidth={1.5}
                    dot={false}
                    activeDot={{ r: 3, fill: s.color, strokeWidth: 0 }}
                    isAnimationActive={false}
                    connectNulls
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          )}

          {!expanded && (
            <div className="mt-1 text-right">
              <span className="text-xs font-mono" style={{ color: '#2a2d38' }}>
                shift+scroll to smooth
              </span>
            </div>
          )}
        </div>
      </div>

      {isExpanded && (
        <ExpandModal onClose={() => setIsExpanded(false)}>
          <CompareChart
            metricName={metricName}
            series={series}
            smoothing={alpha}
            globalHiddenIds={globalHiddenIds}
            expanded
          />
        </ExpandModal>
      )}
    </>
  )
}

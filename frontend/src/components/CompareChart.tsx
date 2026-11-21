import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { Metric } from '../api/types.ts'
import { formatMetricValue } from '../utils/format.ts'
import { MAX_ALPHA, formatLogTick, logTicks, positiveRange, processSeries, useChartOptions } from '../utils/chartOptions.ts'
import type { ChartOptions } from '../utils/chartOptions.ts'
import { coerceAxis, axisLabel, axisMetricName, firstTimestamp, formatXLabel, formatXTick, resolveXs, runHasAxis } from '../utils/xAxis.ts'
import { maxPointsForWidth, zoomQuery } from '../utils/series.ts'
import type { RangeQuery } from '../utils/series.ts'
import { useDragZoom } from '../hooks/useDragZoom.ts'
import { useZoomFetch } from '../hooks/useZoomFetch.ts'
import type { LinkedZoom } from '../hooks/useZoomFetch.ts'
import { useMeasuredWidth } from '../hooks/useMeasuredWidth.ts'
import type { ExternalZoom } from './MetricChart.tsx'
import type { XSource } from '../utils/xAxis.ts'
import { aggregate } from '../utils/grouping.ts'
import type { BandKind } from '../utils/grouping.ts'
import ChartControls from './ChartControls.tsx'
import ExpandModal, { ExpandButton } from './ExpandModal.tsx'

export interface RunSeries {
  runId: string
  name: string
  color: string
  metrics: Metric[]
  /** Enables the x-axis selector; resolved per run (each run's own axis metric / start time). */
  source?: XSource
}

/** A set of runs drawn as one mean line (+ band when it has >= 2 runs). `id` must be a safe dataKey (no dots). */
export interface GroupSeries {
  id: string
  name: string
  color: string
  runs: RunSeries[]
}

interface Props {
  metricName: string
  series: RunSeries[]
  globalHiddenIds?: Set<string>
  expanded?: boolean
  /** When set, runs are aggregated per group (mean line + band) instead of drawn individually. */
  groups?: GroupSeries[]
  band?: BandKind
  /**
   * Self-managed zoom: drag-select refetches the window at full resolution, per run (a step window for step and
   * metric axes, a timestamp window for time axes), then the usual grouping/EMA/log options apply to it.
   */
  fetchRange?: (runId: string, name: string, query: RangeQuery, maxPoints: number) => Promise<Metric[]>
  /** Zoom owned by the parent (series are already windowed). */
  zoom?: ExternalZoom
  /** Zoom shared with sibling charts (each fetches its own window); `undefined` = this chart zooms alone. */
  linkedZoom?: LinkedZoom
  /** Base x-axis kinds offered by the selector (default step, time, rel); GPU charts: ['rel', 'time']. */
  axes?: readonly string[]
  /** Bump to re-fetch the zoomed window of live runs. */
  refreshKey?: number
}

interface Merged {
  rows: Record<string, number | null>[]
  dropped: number
  skipped: number
  nonMonotonic: string[]
  span: number
}

/** Merge run series into [{x, runId: smoothed, runId__raw: raw, …}] for Recharts. Each run resolves its own x. */
function mergeSeriesData(
  series: RunSeries[],
  opts: ChartOptions,
  axis: string,
  metricName: string,
): Merged {
  const xMap = new Map<number, Record<string, number | null>>()
  let dropped = 0
  let skipped = 0
  const nonMonotonic: string[] = []
  for (const run of series) {
    const r = resolveXs(run.metrics, axis, run.source ?? {})
    skipped += r.skipped
    if (r.nonMonotonic) nonMonotonic.push(run.name)
    // r.points is sorted by x, so EMA runs in x order.
    const p = processSeries(r.points.map((pt) => pt.value), opts, metricName)
    dropped += p.dropped
    r.points.forEach((pt, i) => {
      if (!xMap.has(pt.x)) xMap.set(pt.x, { x: pt.x })
      const row = xMap.get(pt.x)!
      row[run.runId] = p.smooth[i]
      row[`${run.runId}__raw`] = p.raw[i]
    })
  }
  const rows = [...xMap.values()].sort((a, b) => (a.x as number) - (b.x as number))
  const span = rows.length > 1 ? (rows[rows.length - 1].x as number) - (rows[0].x as number) : 0
  return { rows, dropped, skipped, nonMonotonic, span }
}

/**
 * Grouped variant of mergeSeriesData. Per run: resolve x, normalize/EMA/log-clip; per group: aggregate onto a
 * common grid. Rows hold `<id>` (mean), `<id>__band` ([lo, hi]) and `<id>__n` (runs covering that x).
 * Single-run groups are drawn as plain lines.
 */
function mergeGroupedData(
  groups: GroupSeries[],
  opts: ChartOptions,
  axis: string,
  metricName: string,
  band: BandKind,
): Merged {
  const xMap = new Map<number, Record<string, number | number[] | null>>()
  let dropped = 0
  let skipped = 0
  const nonMonotonic: string[] = []
  const put = (x: number, key: string, v: number | number[] | null) => {
    if (!xMap.has(x)) xMap.set(x, { x })
    xMap.get(x)![key] = v
  }
  for (const g of groups) {
    const perRun: { x: number; y: number }[][] = []
    for (const run of g.runs) {
      const r = resolveXs(run.metrics, axis, run.source ?? {})
      skipped += r.skipped
      if (r.nonMonotonic) nonMonotonic.push(run.name)
      const p = processSeries(r.points.map((pt) => pt.value), opts, metricName)
      dropped += p.dropped
      const pts: { x: number; y: number }[] = []
      r.points.forEach((pt, i) => {
        const v = p.smooth[i]
        if (v != null && Number.isFinite(v)) pts.push({ x: pt.x, y: v })
      })
      perRun.push(pts)
    }
    if (perRun.length === 1) {
      for (const pt of perRun[0]) put(pt.x, g.id, pt.y)
      continue
    }
    for (const a of aggregate(perRun, band)) {
      put(a.x, g.id, a.mean)
      // Log scale cannot draw a band reaching <= 0: keep it just above zero.
      const lo = opts.logY && a.lo <= 0 ? a.mean * 0.01 : a.lo
      put(a.x, `${g.id}__band`, [lo, a.hi])
      put(a.x, `${g.id}__n`, a.n)
    }
  }
  const rows = [...xMap.values()].sort((a, b) => (a.x as number) - (b.x as number)) as Record<string, number | null>[]
  const span = rows.length > 1 ? (rows[rows.length - 1].x as number) - (rows[0].x as number) : 0
  return { rows, dropped, skipped, nonMonotonic, span }
}

export default function CompareChart({
  metricName,
  series: baseSeries,
  globalHiddenIds,
  expanded = false,
  groups: baseGroups,
  band = 'std',
  fetchRange,
  zoom: externalZoom,
  linkedZoom,
  axes,
  refreshKey,
}: Props) {
  const [opts, setOpts, resetOpts, overridden] = useChartOptions(`compare:${metricName}`)
  const wrapRef = useRef<HTMLDivElement>(null)
  const widthRef = useMeasuredWidth(wrapRef)
  const shownRef = useRef<RunSeries[]>([])
  const { alpha } = opts

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
    setOpts({ alpha: Math.min(MAX_ALPHA, Math.max(0, alpha + (e.deltaY < 0 ? 0.05 : -0.05))) })
  }, [alpha, setOpts])

  const hasAxisSelector = baseSeries.some((s) => s.source)
  const axisNames = useMemo(() => {
    const names = new Set<string>()
    for (const s of baseSeries) for (const n of Object.keys(s.source?.byName ?? {})) names.add(n)
    return [...names].sort()
  }, [baseSeries])
  const axis = hasAxisSelector ? coerceAxis(opts.xAxis, axes) : 'step'

  // Zoom: per run, fetch the window (metric + axis metric) at full resolution and show that instead.
  const self = useZoomFetch<Record<string, { metrics: Metric[]; axisData: Metric[] | null }>>(
    fetchRange && !externalZoom
      ? async (z) => {
          const mp = maxPointsForWidth(widthRef.current)
          const axisName = axisMetricName(z.axis)
          let any = false
          const entries = await Promise.all(
            baseSeries.map(async (run) => {
              const shown = shownRef.current.find((r) => r.runId === run.runId) ?? run
              const pts = resolveXs(shown.metrics, z.axis, shown.source ?? {}).points
              const q = zoomQuery(z, pts, run.source?.startTime ?? firstTimestamp(run.metrics) ?? 0)
              if (!q) return [run.runId, { metrics: [], axisData: null }] as const
              any = true
              const [metrics, axisData] = await Promise.all([
                fetchRange(run.runId, metricName, q, mp),
                axisName ? fetchRange(run.runId, axisName, q, mp) : Promise.resolve(null),
              ])
              return [run.runId, { metrics, axisData }] as const
            }),
          )
          return any ? Object.fromEntries(entries) : null
        }
      : undefined,
    refreshKey,
    linkedZoom,
    axis,
  )
  useEffect(() => {
    if (!linkedZoom && self.zoom && self.zoom.axis !== axis) self.reset()
  }, [axis, self, linkedZoom])
  const zoomed = externalZoom ? externalZoom.active : !!(self.zoom && self.data)
  const zoomable = !!(externalZoom || fetchRange)
  const zoomWin = !externalZoom && self.zoom && self.data ? self.data : null
  const zoomedRun = useCallback((run: RunSeries): RunSeries => {
    const w = zoomWin?.[run.runId]
    if (!w) return run
    const axisName = axisMetricName(axis)
    return {
      ...run,
      metrics: w.metrics,
      source: run.source && {
        ...run.source,
        startTime: run.source.startTime ?? firstTimestamp(run.metrics),
        byName: axisName && w.axisData ? { ...run.source.byName, [axisName]: w.axisData } : run.source.byName,
        interpolate: true,
      },
    }
  }, [zoomWin, axis])
  const series = useMemo(() => baseSeries.map(zoomedRun), [baseSeries, zoomedRun])
  shownRef.current = series
  const groups = useMemo(
    () => baseGroups?.map((g) => ({ ...g, runs: g.runs.map(zoomedRun) })),
    [baseGroups, zoomedRun],
  )
  const onSelect = useMemo(
    () => (zoomable ? (x0: number, x1: number) => (externalZoom ?? { onSelect: self.select }).onSelect({ axis, x0, x1 }) : undefined),
    [zoomable, externalZoom, self.select, axis],
  )
  const resetZoom = externalZoom ? externalZoom.onReset : self.reset
  const { area, handlers } = useDragZoom(onSelect)
  // Runs lacking the chosen axis metric cannot be placed: excluded with a note.
  const excluded = series.filter((s) => !runHasAxis(s.source ?? {}, axis))
  const visibleSeries = series.filter(
    (s) => !isHidden(s.runId) && runHasAxis(s.source ?? {}, axis),
  )
  // Grouped mode: per-group visibility; runs lacking the axis are dropped from their group.
  const visibleGroups = (groups ?? [])
    .filter((g) => !isHidden(g.id))
    .map((g) => ({ ...g, runs: g.runs.filter((r) => runHasAxis(r.source ?? {}, axis)) }))
    .filter((g) => g.runs.length > 0)
  const hasData = groups
    ? visibleGroups.some((g) => g.runs.some((r) => r.metrics.length > 0))
    : visibleSeries.some((s) => s.metrics.length > 0)
  const { rows: data, dropped, skipped, nonMonotonic, span } = useMemo(
    () =>
      hasData
        ? groups
          ? mergeGroupedData(visibleGroups, opts, axis, metricName, band)
          : mergeSeriesData(visibleSeries, opts, axis, metricName)
        : { rows: [] as Record<string, number | null>[], dropped: 0, skipped: 0, nonMonotonic: [] as string[], span: 0 },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [series, groups, band, localHiddenIds, globalHiddenIds, opts, metricName, axis],
  )
  const tickFmt = useMemo(() => formatXTick(axis, span), [axis, span])
  const log = useMemo(() => {
    if (!opts.logY) return null
    const r = positiveRange(data as Record<string, unknown>[])
    return r ? logTicks(r[0], r[1]) : null
  }, [data, opts.logY])
  const legendEntries = groups
    ? groups.map((g) => ({
        id: g.id,
        name: g.name,
        color: g.color,
        badge: g.runs.length > 1 ? `n=${g.runs.length}` : null,
      }))
    : series.map((s) => ({
        id: s.runId,
        name: s.name,
        color: s.color,
        badge:
          !isHidden(s.runId) && s.metrics.length > 0
            ? formatMetricValue(s.metrics[s.metrics.length - 1].value)
            : null,
      }))
  const chartHeight = expanded ? 440 : 200

  return (
    <>
      <div
        ref={wrapRef}
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
          </div>
          <div className="flex items-center gap-2">
            {zoomed && (
              <button
                onClick={resetZoom}
                title="Back to the full range"
                className="text-xs font-mono"
                style={{ background: 'none', border: '1px solid #353840', borderRadius: 4, color: '#f59e0b', padding: '0 6px', cursor: 'pointer' }}
              >
                reset zoom{self.loading ? ' …' : ''}
              </button>
            )}
            {!expanded && <ExpandButton onClick={() => setIsExpanded(true)} />}
          </div>
        </div>
        <ChartControls
          opts={opts}
          onChange={setOpts}
          dropped={dropped}
          axisMetrics={hasAxisSelector ? axisNames : undefined}
          axes={axes}
          overridden={overridden}
          onReset={resetOpts}
        />
        {(excluded.length > 0 || skipped > 0 || nonMonotonic.length > 0) && (
          <div className="text-xs font-mono mb-2" style={{ color: '#f59e0b', fontSize: 10 }}>
            {excluded.length > 0 &&
              `Excluded (no ${axisMetricName(axis)} logged): ${excluded.map((s) => s.name).join(', ')}. `}
            {skipped > 0 && `${skipped} point${skipped === 1 ? '' : 's'} without ${axisLabel(axis)} hidden. `}
            {nonMonotonic.length > 0 &&
              `${axisLabel(axis)} is not monotonic in step for ${nonMonotonic.join(', ')}; points sorted by x.`}
          </div>
        )}
        <div className="flex flex-wrap gap-3 mb-3">
          {legendEntries.map((s) => {
            const hidden = isHidden(s.id)
            return (
              <span
                key={s.id}
                className="text-xs font-mono flex items-center gap-1.5 cursor-pointer select-none"
                style={{ opacity: hidden ? 0.35 : 1 }}
                onClick={() => toggleLocal(s.id)}
                title={hidden ? 'Click to show' : 'Click to hide'}
              >
                <span style={{
                  display: 'inline-block', width: 20, height: 2,
                  backgroundColor: s.color, borderRadius: 1, flexShrink: 0,
                }} />
                <span style={{ color: '#8b8fa8' }}>{s.name}</span>
                {s.badge != null && <span style={{ color: s.color }}>{s.badge}</span>}
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
              <ComposedChart data={data} margin={{ top: 4, right: 4, left: 4, bottom: 0 }} {...handlers} style={onSelect ? { cursor: 'crosshair', userSelect: 'none' } : undefined}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e2128" vertical={false} />
                <XAxis
                  dataKey="x"
                  type="number"
                  domain={['dataMin', 'dataMax']}
                  tickFormatter={tickFmt}
                  tick={{ fontSize: 10, fill: '#4a4e60', fontFamily: 'JetBrains Mono' }}
                  tickLine={false}
                  axisLine={false}
                  minTickGap={48}
                />
                <YAxis
                  tick={{ fontSize: 10, fill: '#4a4e60', fontFamily: 'JetBrains Mono' }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={opts.logY ? formatLogTick : formatMetricValue}
                  width={opts.logY ? 42 : 35}
                  scale={opts.logY ? 'log' : 'auto'}
                  domain={log ? log.domain : opts.logY ? ['dataMin', 'dataMax'] : ['auto', 'auto']}
                  ticks={log?.ticks}
                  allowDataOverflow={opts.logY}
                />
                {log?.minor.map((y) => (
                  <ReferenceLine key={`m${y}`} y={y} stroke="#1e2128" strokeOpacity={0.6} strokeWidth={1} />
                ))}
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
                  labelFormatter={(v) => formatXLabel(axis, Number(v))}
                  formatter={(v: number, key: string, item: { payload?: Record<string, number | null> }) => {
                    const g = groups?.find((x) => x.id === key)
                    if (g) {
                      const n = item.payload?.[`${key}__n`]
                      return [v == null ? '—' : formatMetricValue(v), n != null ? `${g.name} (n=${n})` : g.name]
                    }
                    const run = series.find((s) => s.runId === key)
                    return [v == null ? '—' : formatMetricValue(v), run?.name ?? key]
                  }}
                  itemStyle={{ padding: '1px 0' }}
                  cursor={{ stroke: '#353840', strokeWidth: 1 }}
                />
                {area && <ReferenceArea x1={area[0]} x2={area[1]} fill="#f59e0b" fillOpacity={0.12} stroke="#f59e0b" strokeOpacity={0.4} />}
                {groups && visibleGroups.map((g) => (
                  <Area
                    key={`${g.id}__band`}
                    type="monotone"
                    dataKey={`${g.id}__band`}
                    stroke="none"
                    fill={g.color}
                    fillOpacity={0.18}
                    dot={false}
                    activeDot={false}
                    isAnimationActive={false}
                    tooltipType="none"
                    connectNulls
                  />
                ))}
                {groups && visibleGroups.map((g) => (
                  <Line
                    key={g.id}
                    type="monotone"
                    dataKey={g.id}
                    stroke={g.color}
                    strokeWidth={1.5}
                    dot={false}
                    activeDot={{ r: 3, fill: g.color, strokeWidth: 0 }}
                    isAnimationActive={false}
                    connectNulls
                  />
                ))}
                {!groups && alpha > 0 && visibleSeries.map((s) => (
                  <Line
                    key={`${s.runId}__raw`}
                    type="monotone"
                    dataKey={`${s.runId}__raw`}
                    stroke={s.color}
                    strokeOpacity={0.25}
                    strokeWidth={1}
                    dot={false}
                    activeDot={false}
                    isAnimationActive={false}
                    tooltipType="none"
                    connectNulls
                  />
                ))}
                {!groups && visibleSeries.map((s) => (
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
              </ComposedChart>
            </ResponsiveContainer>
          )}

          {!expanded && (
            <div className="mt-1 text-right">
              <span className="text-xs font-mono" style={{ color: '#2a2d38' }}>
                {zoomable ? 'drag to zoom · ' : ''}shift+scroll to smooth{self.failed ? ' · zoom fetch failed' : ''}
              </span>
            </div>
          )}
        </div>
      </div>

      {isExpanded && (
        <ExpandModal onClose={() => setIsExpanded(false)}>
          <CompareChart
            metricName={metricName}
            series={baseSeries}
            globalHiddenIds={globalHiddenIds}
            groups={baseGroups}
            band={band}
            fetchRange={fetchRange}
            zoom={externalZoom}
            linkedZoom={linkedZoom}
            axes={axes}
            refreshKey={refreshKey}
            expanded
          />
        </ExpandModal>
      )}
    </>
  )
}

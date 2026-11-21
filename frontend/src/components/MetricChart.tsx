import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { Metric, RunEvent } from '../api/types.ts'
import { formatMetricValue } from '../utils/format.ts'
import { MAX_ALPHA, formatLogTick, logTicks, positiveRange, processSeries, useChartOptions } from '../utils/chartOptions.ts'
import { coerceAxis, axisLabel, axisMetricName, eventX, firstTimestamp, formatXLabel, formatXTick, resolveXs } from '../utils/xAxis.ts'
import type { XPoint, XSource } from '../utils/xAxis.ts'
import { maxPointsForWidth, zoomQuery } from '../utils/series.ts'
import type { RangeQuery, XZoom } from '../utils/series.ts'
import { useDragZoom } from '../hooks/useDragZoom.ts'
import { useZoomFetch } from '../hooks/useZoomFetch.ts'
import { useMeasuredWidth } from '../hooks/useMeasuredWidth.ts'
import ChartControls from './ChartControls.tsx'
import ExpandModal, { ExpandButton } from './ExpandModal.tsx'

interface Props {
  name: string
  data: Metric[]
  lineColor?: string
  /** Events to render as vertical marker lines. */
  events?: RunEvent[]
  /** When true, renders taller (for use inside ExpandModal). */
  expanded?: boolean
  /** Enables the x-axis selector: the run's metrics by name (for metric-as-x) and start time. */
  source?: XSource
  /** Base x-axis kinds offered by the selector (default step, time, rel); GPU charts: ['rel', 'time']. */
  axes?: readonly string[]
  /**
   * Self-managed zoom: drag-select on the chart fetches that window at full resolution through this
   * callback (one call per series: the metric and, on a metric x-axis, the axis metric).
   */
  fetchRange?: (name: string, query: RangeQuery, maxPoints: number) => Promise<Metric[]>
  /** Zoom owned by the parent (e.g. a section that zooms several charts together); `data` is already windowed. */
  zoom?: ExternalZoom
  /** Bump to re-fetch the zoomed window of a live run. */
  refreshKey?: number
}

export interface ExternalZoom {
  active: boolean
  onSelect: (zoom: XZoom) => void
  onReset: () => void
}

interface Row {
  x: number
  value: number | null
  raw: number | null
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
  logY,
  showRaw,
  axis,
  span,
  onSelect,
}: {
  onSelect?: (x0: number, x1: number) => void
  axis: string
  span: number
  data: Row[]
  logY: boolean
  showRaw: boolean
  lineColor: string
  height: number
  /** Events already mapped to x positions. */
  events?: { x: number; severity: string }[]
}) {
  // Deduplicate by x (keep first per position to avoid overlapping lines)
  const eventMarkers = useMemo(() => {
    if (!events || events.length === 0) return []
    const seen = new Set<number>()
    return events.filter((e) => {
      if (seen.has(e.x)) return false
      seen.add(e.x)
      return true
    })
  }, [events])
  const tickFmt = useMemo(() => formatXTick(axis, span), [axis, span])
  const log = useMemo(() => {
    if (!logY) return null
    const r = positiveRange(data as unknown as Record<string, unknown>[])
    return r ? logTicks(r[0], r[1]) : null
  }, [data, logY])

  const { area, handlers } = useDragZoom(onSelect)

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 4, right: 4, left: 4, bottom: 0 }} {...handlers} style={onSelect ? { cursor: 'crosshair', userSelect: 'none' } : undefined}>
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
          tickFormatter={logY ? formatLogTick : formatMetricValue}
          width={logY ? 42 : 35}
          scale={logY ? 'log' : 'auto'}
          domain={log ? log.domain : logY ? ['dataMin', 'dataMax'] : ['auto', 'auto']}
          ticks={log?.ticks}
          allowDataOverflow={logY}
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
          labelStyle={{ color: '#8b8fa8', marginBottom: 2 }}
          itemStyle={{ color: lineColor }}
          labelFormatter={(v) => formatXLabel(axis, Number(v))}
          formatter={(v: number) => [formatMetricValue(v), '']}
          cursor={{ stroke: '#353840', strokeWidth: 1 }}
        />
        {area && <ReferenceArea x1={area[0]} x2={area[1]} fill="#f59e0b" fillOpacity={0.12} stroke="#f59e0b" strokeOpacity={0.4} />}
        {eventMarkers.map((ev, i) => (
          <ReferenceLine
            key={i}
            x={ev.x}
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
        {showRaw && (
          <Line
            type="monotone"
            dataKey="raw"
            stroke={lineColor}
            strokeOpacity={0.25}
            strokeWidth={1}
            dot={false}
            activeDot={false}
            isAnimationActive={false}
            tooltipType="none"
          />
        )}
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
  source,
  axes,
  fetchRange,
  zoom: externalZoom,
  refreshKey,
}: Props) {
  const [opts, setOpts, resetOpts, overridden] = useChartOptions(`metric:${name}`)
  const { alpha } = opts
  const [isExpanded, setIsExpanded] = useState(false)
  const [showLocalEvents, setShowLocalEvents] = useState(true)
  const containerRef = useRef<HTMLDivElement>(null)
  const widthRef = useMeasuredWidth(containerRef)

  const src: XSource = useMemo(() => source ?? {}, [source])
  const axisNames = useMemo(
    () => (source?.byName ? Object.keys(source.byName).sort() : []),
    [source],
  )
  // A stored metric axis this run does not have falls back to step (with a note).
  const wanted = source ? coerceAxis(opts.xAxis, axes) : 'step'
  const axisMissing = axisMetricName(wanted) != null && !(src.byName?.[axisMetricName(wanted)!]?.length)
  const axis = axisMissing ? coerceAxis('step', axes) : wanted

  // Zoom: the window is refetched at full resolution (self-managed) or supplied by the parent (external).
  const pointsRef = useRef<XPoint[]>([])
  const self = useZoomFetch<{ data: Metric[]; axisData: Metric[] | null }>(
    fetchRange && !externalZoom
      ? async (z) => {
          const q = zoomQuery(z, pointsRef.current, src.startTime ?? firstTimestamp(data) ?? 0)
          if (!q) return null
          const mp = maxPointsForWidth(widthRef.current)
          const axisName = axisMetricName(z.axis)
          const [series, axisData] = await Promise.all([
            fetchRange(name, q, mp),
            axisName ? fetchRange(axisName, q, mp) : Promise.resolve(null),
          ])
          return { data: series, axisData }
        }
      : undefined,
    refreshKey,
  )
  useEffect(() => {
    if (self.zoom && self.zoom.axis !== axis) self.reset() // x-axis changed: the old window means something else
  }, [axis, self])
  const zoomed = externalZoom ? externalZoom.active : !!(self.zoom && self.data)
  const zoomable = !!(externalZoom || fetchRange)
  const shownData = !externalZoom && self.zoom && self.data ? self.data.data : data
  const shownSrc: XSource = useMemo(() => {
    const axisName = axisMetricName(axis)
    if (externalZoom || !self.zoom || !self.data) return src
    return {
      ...src,
      startTime: src.startTime ?? firstTimestamp(data),
      byName: axisName && self.data.axisData ? { ...src.byName, [axisName]: self.data.axisData } : src.byName,
      interpolate: true,
    }
  }, [src, self.zoom, self.data, axis, data, externalZoom])
  const onSelect = useMemo(
    () => (zoomable ? (x0: number, x1: number) => (externalZoom ?? { onSelect: self.select }).onSelect({ axis, x0, x1 }) : undefined),
    [zoomable, externalZoom, self.select, axis],
  )
  const resetZoom = externalZoom ? externalZoom.onReset : self.reset

  const { rows, dropped, skipped, nonMonotonic, span } = useMemo(() => {
    const { points, skipped, nonMonotonic } = resolveXs(shownData, axis, shownSrc)
    pointsRef.current = points
    // Points are sorted by x, so EMA runs in x order.
    const { raw, smooth, dropped } = processSeries(
      points.map((p) => p.value),
      opts,
      name,
    )
    const rows: Row[] = points.map((p, i) => ({ x: p.x, value: smooth[i], raw: raw[i] }))
    const span = points.length > 1 ? points[points.length - 1].x - points[0].x : 0
    return { rows, dropped, skipped, nonMonotonic, span }
  }, [shownData, opts, name, axis, shownSrc])

  const { markers, unmapped } = useMemo(() => {
    const markers: { x: number; severity: string }[] = []
    let unmapped = 0
    for (const ev of showLocalEvents ? (events ?? []) : []) {
      const x = eventX(ev, axis, shownSrc, firstTimestamp(shownData))
      if (x == null) {
        if (ev.step != null) unmapped++ // step-less events simply have no place on step/metric axes
      } else markers.push({ x, severity: ev.severity })
    }
    return { markers, unmapped }
  }, [events, showLocalEvents, axis, shownSrc, shownData])
  const last = useMemo(() => {
    for (let i = rows.length - 1; i >= 0; i--) if (rows[i].value != null) return rows[i].value
    return null
  }, [rows])

  const onWheel = useCallback((e: React.WheelEvent) => {
    if (!e.shiftKey) return
    e.preventDefault()
    setOpts({ alpha: Math.min(MAX_ALPHA, Math.max(0, alpha + (e.deltaY < 0 ? 0.05 : -0.05))) })
  }, [alpha, setOpts])

  const chartHeight = expanded ? 440 : 160

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

        {data.length > 0 && (
          <ChartControls
            opts={opts}
            onChange={setOpts}
            dropped={dropped}
            axisMetrics={source ? axisNames : undefined}
            axes={axes}
            overridden={overridden}
            onReset={resetOpts}
          />
        )}
        {(axisMissing || skipped > 0 || nonMonotonic || unmapped > 0) && (
          <div className="text-xs font-mono mb-1" style={{ color: '#f59e0b', fontSize: 10 }}>
            {axisMissing && `no ${axisMetricName(wanted)} logged for this run, showing step. `}
            {skipped > 0 && `${skipped} point${skipped === 1 ? '' : 's'} without ${axisLabel(axis)} hidden. `}
            {nonMonotonic && `${axisLabel(axis)} is not monotonic in step; points sorted by x. `}
            {unmapped > 0 && `${unmapped} event marker${unmapped === 1 ? '' : 's'} hidden (no ${axisLabel(axis)} at that step).`}
          </div>
        )}

        {data.length === 0 ? (
          <div
            className="flex items-center justify-center text-xs font-mono"
            style={{ height: 160, color: '#4a4e60' }}
          >
            no data
          </div>
        ) : (
          <Chart
            data={rows}
            lineColor={lineColor}
            height={chartHeight}
            events={markers}
            axis={axis}
            span={span}
            logY={opts.logY}
            showRaw={alpha > 0}
            onSelect={onSelect}
          />
        )}

        {!expanded && (
          <div className="mt-1 text-right">
            <span className="text-xs font-mono" style={{ color: '#2a2d38' }}>
              {zoomable ? 'drag to zoom · ' : ''}shift+scroll to smooth{self.failed ? ' · zoom fetch failed' : ''}
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
            source={source}
            axes={axes}
            fetchRange={fetchRange}
            zoom={externalZoom}
            refreshKey={refreshKey}
            expanded
          />
        </ExpandModal>
      )}
    </>
  )
}

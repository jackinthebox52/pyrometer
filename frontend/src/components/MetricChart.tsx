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
import { formatMetricValue, Y_AXIS_WIDTH } from '../utils/format.ts'
import { MAX_ALPHA, formatLogTick, logTicks, positiveRange, processSeries, useChartOptions } from '../utils/chartOptions.ts'
import { coerceAxis, axisLabel, axisMetricName, eventX, firstTimestamp, formatXLabel, formatXTick, resolveXs } from '../utils/xAxis.ts'
import type { XPoint, XSource } from '../utils/xAxis.ts'
import { maxPointsForWidth, zoomQuery } from '../utils/series.ts'
import type { RangeQuery, XZoom } from '../utils/series.ts'
import { useDragZoom } from '../hooks/useDragZoom.ts'
import { useZoomFetch } from '../hooks/useZoomFetch.ts'
import { useMeasuredWidth } from '../hooks/useMeasuredWidth.ts'
import ChartControls, { ChartChips } from './ChartControls.tsx'
import Icon from './Icon.tsx'
import { CHART, N, RED, SERIES } from '../theme.ts'
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

/** Anomalies are red; every other event is a neutral marker. */
function eventColor(anomaly: boolean): string {
  return anomaly ? RED : N.n6
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
  events?: { x: number; anomaly: boolean }[]
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
      <LineChart data={data} margin={{ top: 4, right: 18, left: 4, bottom: 0 }} {...handlers} style={onSelect ? { cursor: 'crosshair', userSelect: 'none' } : undefined}>
        <CartesianGrid strokeDasharray="2 4" stroke={CHART.grid} vertical={false} />
        <XAxis
          dataKey="x"
          type="number"
          domain={['dataMin', 'dataMax']}
          tickFormatter={tickFmt}
          tick={CHART.tick}
          tickLine={false}
          axisLine={{ stroke: CHART.axis }}
          minTickGap={48}
        />
        <YAxis
          tick={CHART.tick}
          tickLine={false}
          axisLine={false}
          tickFormatter={logY ? formatLogTick : formatMetricValue}
          width={Y_AXIS_WIDTH}
          scale={logY ? 'log' : 'auto'}
          domain={log ? log.domain : logY ? ['dataMin', 'dataMax'] : ['auto', 'auto']}
          ticks={log?.ticks}
          allowDataOverflow={logY}
        />
        {log?.minor.map((y) => (
          <ReferenceLine key={`m${y}`} y={y} stroke={CHART.grid} strokeOpacity={0.6} strokeWidth={1} />
        ))}
        <Tooltip
          {...CHART.tooltip}
          labelFormatter={(v) => formatXLabel(axis, Number(v))}
          formatter={(v: number) => [formatMetricValue(v), '']}
          cursor={{ stroke: N.n5, strokeWidth: 1 }}
        />
        {area && <ReferenceArea x1={area[0]} x2={area[1]} fill={N.n8} fillOpacity={0.08} stroke={N.n7} strokeWidth={1} />}
        {eventMarkers.map((ev, i) => (
          <ReferenceLine
            key={i}
            x={ev.x}
            stroke={eventColor(ev.anomaly)}
            strokeWidth={1}
            strokeDasharray="3 3"
          />
        ))}
        {showRaw && (
          <Line
            type="monotone"
            dataKey="raw"
            stroke={lineColor}
            strokeOpacity={0.3}
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
  lineColor = SERIES,
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
    const markers: { x: number; anomaly: boolean }[] = []
    let unmapped = 0
    for (const ev of showLocalEvents ? (events ?? []) : []) {
      const x = eventX(ev, axis, shownSrc, firstTimestamp(shownData))
      if (x == null) {
        if (ev.step != null) unmapped++ // step-less events simply have no place on step/metric axes
      } else markers.push({ x, anomaly: ev.event_type === 'anomaly_detected' })
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

  const chartHeight = expanded ? 440 : 190

  return (
    <>
      <div
        ref={containerRef}
        className="group rounded-sm border border-py-border bg-py-surface p-3"
        onWheel={onWheel}
      >
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <span className="num truncate text-xs text-py-soft" title={name}>
              {name}
            </span>
            <ChartChips opts={opts} />
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {zoomed && (
              <button
                type="button"
                onClick={resetZoom}
                title="Back to the full range"
                className="h-6 rounded-sm border border-py-border-hi bg-py-surface px-2 text-xs text-py-soft transition-colors duration-150 ease-out hover:bg-py-elevated hover:text-py-text"
              >
                Reset zoom{self.loading ? '…' : ''}
              </button>
            )}
            {last !== null && <span className="num mr-1 text-[13px] text-py-text">{formatMetricValue(last)}</span>}
            {!expanded && events && events.length > 0 && (
              <button
                type="button"
                onClick={() => setShowLocalEvents((v) => !v)}
                title={showLocalEvents ? 'Hide event markers' : 'Show event markers'}
                aria-label={showLocalEvents ? 'Hide event markers' : 'Show event markers'}
                aria-pressed={showLocalEvents}
                className="inline-flex h-6 w-6 items-center justify-center rounded-sm text-py-muted transition-colors duration-150 ease-out hover:bg-py-elevated hover:text-py-text"
              >
                <Icon name={showLocalEvents ? 'eye' : 'eye-off'} size={14} />
              </button>
            )}
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
            {!expanded && <ExpandButton onClick={() => setIsExpanded(true)} />}
          </div>
        </div>

        {(axisMissing || skipped > 0 || nonMonotonic || unmapped > 0) && (
          <div className="mb-1 text-[11px] text-py-muted">
            {axisMissing && `no ${axisMetricName(wanted)} logged for this run, showing step. `}
            {skipped > 0 && `${skipped} point${skipped === 1 ? '' : 's'} without ${axisLabel(axis)} hidden. `}
            {nonMonotonic && `${axisLabel(axis)} is not monotonic in step; points sorted by x. `}
            {unmapped > 0 && `${unmapped} event marker${unmapped === 1 ? '' : 's'} hidden (no ${axisLabel(axis)} at that step).`}
          </div>
        )}

        {data.length === 0 ? (
          <div className="flex items-center justify-center text-xs text-py-faint" style={{ height: chartHeight }}>
            No data logged yet
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

        {!expanded && self.failed && <div className="mt-1 text-right text-[11px] text-py-faint">Zoom fetch failed.</div>}
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

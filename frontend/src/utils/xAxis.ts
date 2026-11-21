import type { Metric } from '../api/types.ts'

/** 'step' | 'time' (wall clock) | 'rel' (time since run start) | 'metric:<name>' (any logged metric). */
export type XAxis = string

export const METRIC_PREFIX = 'metric:'

/** Per-run data needed to resolve non-step axes. */
export interface XSource {
  /** All of the run's metrics by name (sorted by step); lets a metric act as the x-axis. */
  byName?: Record<string, Metric[]>
  /** Run start, unix seconds. Falls back to the first point's timestamp. */
  startTime?: number
  /**
   * The axis metric was downsampled by the server, so it rarely has a value at exactly the step of another
   * (separately downsampled) series: place those points by linear interpolation between the neighbouring
   * axis samples instead of skipping them. Points outside the axis metric's step range are still skipped.
   */
  interpolate?: boolean
}

export interface XPoint {
  x: number
  value: number
  step: number
}

/** Value of a step-sorted metric at `step`: exact if logged, else (when `interpolate`) linear between neighbours. */
function axisValueAt(arr: Metric[], step: number, exact: Map<number, number>, interpolate: boolean): number | null {
  const hit = exact.get(step)
  if (hit != null || !interpolate || arr.length === 0) return hit ?? null
  if (step < arr[0].step || step > arr[arr.length - 1].step) return null
  let lo = 0
  let hi = arr.length - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (arr[mid].step <= step) lo = mid
    else hi = mid
  }
  const a = arr[lo]
  const b = arr[hi]
  if (b.step === a.step) return a.value
  return a.value + ((b.value - a.value) * (step - a.step)) / (b.step - a.step)
}

export function axisMetricName(axis: XAxis): string | null {
  return axis.startsWith(METRIC_PREFIX) ? axis.slice(METRIC_PREFIX.length) : null
}

export function axisLabel(axis: XAxis): string {
  if (axis === 'time') return 'time'
  if (axis === 'rel') return 'elapsed'
  return axisMetricName(axis) ?? 'step'
}

/** True if this run can provide the axis (step/time axes always can). */
export function runHasAxis(src: XSource, axis: XAxis): boolean {
  const name = axisMetricName(axis)
  return name == null || (src.byName?.[name]?.length ?? 0) > 0
}

/** Timestamp of the lowest-step point: the shared fallback for "run start" when `src.startTime` is unknown. */
export function firstTimestamp(data: Metric[]): number | undefined {
  let best: Metric | undefined
  for (const d of data) if (best === undefined || d.step < best.step) best = d
  return best?.timestamp
}

/**
 * Resolve each point's x for the chosen axis, sorted by x (stable). For metric axes the axis metric is
 * joined on step; points without an axis value at that step are skipped (counted in `skipped`).
 * `nonMonotonic` is true if the axis value ever decreases as step increases.
 */
export function resolveXs(
  data: Metric[],
  axis: XAxis,
  src: XSource,
): { points: XPoint[]; skipped: number; nonMonotonic: boolean } {
  const byStep = [...data].sort((a, b) => a.step - b.step)
  const metric = axisMetricName(axis)
  let skipped = 0
  const points: XPoint[] = []
  if (metric != null) {
    const lookup = new Map<number, number>()
    const axisSeries = src.byName?.[metric] ?? []
    for (const m of axisSeries) lookup.set(m.step, m.value)
    for (const d of byStep) {
      const x = axisValueAt(axisSeries, d.step, lookup, !!src.interpolate)
      if (x == null || !Number.isFinite(x)) skipped++
      else points.push({ x, value: d.value, step: d.step })
    }
  } else if (axis === 'time' || axis === 'rel') {
    const start = src.startTime ?? firstTimestamp(data) ?? 0
    for (const d of byStep) {
      points.push({ x: axis === 'time' ? d.timestamp : d.timestamp - start, value: d.value, step: d.step })
    }
  } else {
    for (const d of byStep) points.push({ x: d.step, value: d.value, step: d.step })
  }
  let nonMonotonic = false
  for (let i = 1; i < points.length; i++) {
    if (points[i].x < points[i - 1].x) {
      nonMonotonic = true
      break
    }
  }
  if (nonMonotonic) points.sort((a, b) => a.x - b.x)
  return { points, skipped, nonMonotonic }
}

/**
 * Map an event (by step / timestamp) to an x position, or null if it cannot be placed.
 * Time axes ('time', 'rel') place any event by its timestamp, step-less ones included. 'rel' measures from
 * `src.startTime`, falling back to `fallbackStart` (the chart's first point timestamp, as resolveXs does).
 * Step axes need the event's step; metric axes use the axis value at the latest logged step <= the event's step.
 */
export function eventX(
  ev: { step: number | null; timestamp: number },
  axis: XAxis,
  src: XSource,
  fallbackStart?: number,
): number | null {
  const metric = axisMetricName(axis)
  if (metric != null) {
    if (ev.step == null) return null
    const arr = src.byName?.[metric] ?? []
    let lo = 0
    let hi = arr.length - 1
    let found = -1
    while (lo <= hi) {
      const mid = (lo + hi) >> 1
      if (arr[mid].step <= ev.step) {
        found = mid
        lo = mid + 1
      } else hi = mid - 1
    }
    return found >= 0 ? arr[found].value : null
  }
  if (axis === 'time') return ev.timestamp
  if (axis === 'rel') {
    const start = src.startTime ?? fallbackStart
    return start != null ? ev.timestamp - start : null
  }
  return ev.step
}

function compact(v: number): string {
  const abs = Math.abs(v)
  if (abs >= 1e9) return `${+(v / 1e9).toPrecision(3)}B`
  if (abs >= 1e6) return `${+(v / 1e6).toPrecision(3)}M`
  if (abs >= 1e4) return `${+(v / 1e3).toPrecision(3)}k`
  if (Number.isInteger(v)) return String(v)
  return abs >= 1 ? v.toFixed(2) : v.toPrecision(3)
}

function formatElapsed(secs: number): string {
  const s = Math.round(Math.max(0, secs))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m${s % 60 ? ` ${s % 60}s` : ''}`
  const h = Math.floor(m / 60)
  return `${h}h${m % 60 ? ` ${m % 60}m` : ''}`
}

/** Tick label: wall-clock HH:MM (date when the span exceeds a day), elapsed h/m/s, or compact numbers. */
export function formatXTick(axis: XAxis, spanSeconds = 0): (v: number) => string {
  if (axis === 'time') {
    return (v) => {
      const d = new Date(v * 1000)
      const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      return spanSeconds > 86400 ? `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${t}` : t
    }
  }
  if (axis === 'rel') return formatElapsed
  return compact
}

/** Tooltip header for an x value. */
export function formatXLabel(axis: XAxis, v: number): string {
  if (axis === 'time') return new Date(v * 1000).toLocaleString()
  if (axis === 'rel') return `elapsed ${formatElapsed(v)}`
  return `${axisLabel(axis)} ${Number.isInteger(v) ? v : compact(v)}`
}

/**
 * Restrict the x-axis to the kinds a chart offers (e.g. GPU charts have no steps: elapsed or wall time). A stored
 * axis outside `axes` (and not a metric axis, which the caller validates) becomes the first allowed kind.
 */
export function coerceAxis(axis: XAxis, axes?: readonly string[]): XAxis {
  if (!axes || axes.length === 0 || axisMetricName(axis) != null || axes.includes(axis)) return axis
  return axes[0]
}

/** Convert a drag selection on a time-based chart into a timestamp window (Unix seconds). */
export function timeWindow(axis: XAxis, x0: number, x1: number, startTime: number): { t_min: number; t_max: number } {
  const off = axis === 'time' ? 0 : startTime
  return { t_min: off + Math.min(x0, x1), t_max: off + Math.max(x0, x1) }
}

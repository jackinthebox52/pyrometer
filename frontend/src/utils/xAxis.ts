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
}

export interface XPoint {
  x: number
  value: number
  step: number
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
    for (const m of src.byName?.[metric] ?? []) lookup.set(m.step, m.value)
    for (const d of byStep) {
      const x = lookup.get(d.step)
      if (x == null || !Number.isFinite(x)) skipped++
      else points.push({ x, value: d.value, step: d.step })
    }
  } else if (axis === 'time' || axis === 'rel') {
    const start = src.startTime ?? byStep[0]?.timestamp ?? 0
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
 * Metric axes use the axis value at the latest logged step <= the event's step.
 */
export function eventX(
  ev: { step: number | null; timestamp: number },
  axis: XAxis,
  src: XSource,
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
  if (axis === 'rel') return src.startTime != null ? ev.timestamp - src.startTime : null
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

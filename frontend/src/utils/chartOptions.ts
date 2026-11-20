import { useCallback, useEffect, useState } from 'react'

export type Normalization = 'none' | 'first' | 'best'

export interface ChartOptions {
  logY: boolean
  /** EMA smoothing factor, 0 (off) to 0.99. */
  alpha: number
  norm: Normalization
  /** x-axis: 'step', 'time', 'rel' or 'metric:<name>' (see utils/xAxis.ts). */
  xAxis: string
}

export const MAX_ALPHA = 0.99
const STORAGE_PREFIX = 'pyrometer.chart.'

/** Metrics where lower is better, used to pick "best" for % of best normalization. */
const LOWER_IS_BETTER = /loss|err|perplex|ppl|latency|time|grad_norm|nll/i

function load(key: string): Partial<ChartOptions> {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + key)
    if (!raw) return {}
    const o = JSON.parse(raw) as Partial<ChartOptions>
    const out: Partial<ChartOptions> = {}
    if (typeof o.logY === 'boolean') out.logY = o.logY
    if (typeof o.alpha === 'number') out.alpha = Math.min(MAX_ALPHA, Math.max(0, o.alpha))
    if (o.norm === 'none' || o.norm === 'first' || o.norm === 'best') out.norm = o.norm
    if (typeof o.xAxis === 'string' && o.xAxis) out.xAxis = o.xAxis
    return out
  } catch {
    return {}
  }
}

function save(key: string, opts: ChartOptions): void {
  try {
    localStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(opts))
  } catch {
    /* storage unavailable: options simply do not persist */
  }
}

/**
 * Chart options persisted per key in localStorage (key = scope + metric name).
 * `defaultAlpha` is used when nothing is stored, and re-applied if it changes later
 * (the compare view's global smoothing slider).
 */
export function useChartOptions(
  key: string,
  defaultAlpha = 0,
): [ChartOptions, (patch: Partial<ChartOptions>) => void] {
  const [opts, setOpts] = useState<ChartOptions>(() => ({
    logY: false,
    alpha: defaultAlpha,
    norm: 'none',
    xAxis: 'step',
    ...load(key),
  }))

  const update = useCallback(
    (patch: Partial<ChartOptions>) => {
      setOpts((prev) => {
        const next = { ...prev, ...patch }
        if (next.alpha !== prev.alpha) next.alpha = Math.min(MAX_ALPHA, Math.max(0, next.alpha))
        save(key, next)
        return next
      })
    },
    [key],
  )

  // Follow the external default when it changes after mount.
  const [prevDefault, setPrevDefault] = useState(defaultAlpha)
  useEffect(() => {
    if (defaultAlpha !== prevDefault) {
      setPrevDefault(defaultAlpha)
      update({ alpha: defaultAlpha })
    }
  }, [defaultAlpha, prevDefault, update])

  return [opts, update]
}

/** Normalize values (null stays null). Zero reference values yield null. */
export function normalize(
  values: (number | null)[],
  norm: Normalization,
  metricName: string,
): (number | null)[] {
  if (norm === 'none') return values
  const finite = values.filter((v): v is number => v != null && Number.isFinite(v))
  if (finite.length === 0) return values
  let ref: number
  if (norm === 'first') {
    ref = finite[0]
  } else {
    ref = LOWER_IS_BETTER.test(metricName) ? Math.min(...finite) : Math.max(...finite)
  }
  if (ref === 0) return values.map(() => null)
  return values.map((v) => {
    if (v == null || !Number.isFinite(v)) return null
    // first: v/first (1.0 = start). best: v as a percentage of the best value seen.
    return norm === 'first' ? v / ref : (v / ref) * 100
  })
}

/** EMA that skips null/non-finite entries (they stay null in the output). */
export function ema(values: (number | null)[], alpha: number): (number | null)[] {
  if (alpha === 0) return values
  let prev: number | null = null
  return values.map((v) => {
    if (v == null || !Number.isFinite(v)) return null
    prev = prev == null ? v : alpha * prev + (1 - alpha) * v
    return prev
  })
}

/** Log scale cannot show values <= 0: replace them with null and report how many were dropped. */
export function clipForLog(values: (number | null)[]): { values: (number | null)[]; dropped: number } {
  let dropped = 0
  const out = values.map((v) => {
    if (v == null) return null
    if (v <= 0) {
      dropped++
      return null
    }
    return v
  })
  return { values: out, dropped }
}

/** Full pipeline. Returns the raw (normalized) and smoothed series plus count of values hidden by log scale. */
export function processSeries(
  values: number[],
  opts: ChartOptions,
  metricName: string,
): { raw: (number | null)[]; smooth: (number | null)[]; dropped: number } {
  const normed = normalize(values, opts.norm, metricName)
  const smoothed = ema(normed, opts.alpha)
  if (!opts.logY) return { raw: normed, smooth: smoothed, dropped: 0 }
  const r = clipForLog(normed)
  const s = clipForLog(smoothed)
  return { raw: r.values, smooth: s.values, dropped: Math.max(r.dropped, s.dropped) }
}

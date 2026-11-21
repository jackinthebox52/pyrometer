import { useCallback, useState, useSyncExternalStore } from 'react'

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

/** Built-in defaults, used when neither a global default nor a per-chart override is set. */
export const BUILTIN_OPTIONS: ChartOptions = { logY: false, alpha: 0, norm: 'none', xAxis: 'step' }

/** Global defaults (one record, shared by the run page and the compare view). */
const DEFAULTS_KEY = 'pyrometer.chartdefaults'
/** Per-chart overrides: only the fields that differ from the defaults are stored. */
const OVERRIDE_PREFIX = 'pyrometer.chart.v2.'
/** Pre-v2 per-chart storage: a full ChartOptions record per key. Migrated lazily on first load. */
const LEGACY_PREFIX = 'pyrometer.chart.'

/** Metrics where lower is better, used to pick "best" for % of best normalization. */
const LOWER_IS_BETTER = /loss|err|perplex|ppl|latency|time|grad_norm|nll/i

/** Keep only well-formed fields of a parsed storage value. */
export function sanitizeOptions(o: unknown): Partial<ChartOptions> {
  const out: Partial<ChartOptions> = {}
  if (!o || typeof o !== 'object') return out
  const r = o as Record<string, unknown>
  if (typeof r.logY === 'boolean') out.logY = r.logY
  if (typeof r.alpha === 'number' && Number.isFinite(r.alpha)) out.alpha = Math.min(MAX_ALPHA, Math.max(0, r.alpha))
  if (r.norm === 'none' || r.norm === 'first' || r.norm === 'best') out.norm = r.norm
  if (typeof r.xAxis === 'string' && r.xAxis) out.xAxis = r.xAxis
  return out
}

/** Effective options: built-in < global defaults < per-chart overrides. */
export function resolveOptions(defaults: Partial<ChartOptions>, overrides: Partial<ChartOptions>): ChartOptions {
  return { ...BUILTIN_OPTIONS, ...defaults, ...overrides }
}

/**
 * Apply a patch to a chart's overrides. A patched value equal to the current default drops the override
 * (the chart then follows the default again); anything else is stored as an override.
 */
export function applyPatch(
  overrides: Partial<ChartOptions>,
  patch: Partial<ChartOptions>,
  defaults: ChartOptions,
): Partial<ChartOptions> {
  const next: Partial<ChartOptions> = { ...overrides }
  for (const k of Object.keys(patch) as (keyof ChartOptions)[]) {
    let v = patch[k]
    if (v === undefined) continue
    if (k === 'alpha') v = Math.min(MAX_ALPHA, Math.max(0, v as number))
    if (v === defaults[k]) delete next[k]
    else (next as Record<string, unknown>)[k] = v
  }
  return next
}

/** Convert a legacy full record into overrides: fields equal to the built-in defaults are dropped. */
export function migrateLegacy(raw: unknown): Partial<ChartOptions> {
  const clean = sanitizeOptions(raw)
  const out: Partial<ChartOptions> = {}
  for (const k of Object.keys(clean) as (keyof ChartOptions)[]) {
    if (clean[k] !== BUILTIN_OPTIONS[k]) (out as Record<string, unknown>)[k] = clean[k]
  }
  return out
}

function readJson(key: string): unknown {
  try {
    const raw = localStorage.getItem(key)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* storage unavailable: options simply do not persist */
  }
}

function removeKey(key: string): void {
  try {
    localStorage.removeItem(key)
  } catch {
    /* ignore */
  }
}

function loadOverrides(key: string): Partial<ChartOptions> {
  const v2 = readJson(OVERRIDE_PREFIX + key)
  if (v2 != null) return sanitizeOptions(v2)
  const legacy = readJson(LEGACY_PREFIX + key)
  if (legacy == null) return {}
  const migrated = migrateLegacy(legacy)
  writeJson(OVERRIDE_PREFIX + key, migrated)
  removeKey(LEGACY_PREFIX + key)
  return migrated
}

function saveOverrides(key: string, o: Partial<ChartOptions>): void {
  if (Object.keys(o).length === 0) removeKey(OVERRIDE_PREFIX + key)
  else writeJson(OVERRIDE_PREFIX + key, o)
}

// --- global defaults: a tiny external store so every chart follows a change immediately ---

let defaultsCache: Partial<ChartOptions> | null = null
const listeners = new Set<() => void>()

function getDefaultsSnapshot(): Partial<ChartOptions> {
  if (defaultsCache == null) defaultsCache = sanitizeOptions(readJson(DEFAULTS_KEY))
  return defaultsCache
}

function subscribeDefaults(cb: () => void): () => void {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

/** Update the global defaults. */
export function setChartDefaults(patch: Partial<ChartOptions>): void {
  const next = { ...getDefaultsSnapshot(), ...patch }
  if (next.alpha !== undefined) next.alpha = Math.min(MAX_ALPHA, Math.max(0, next.alpha))
  defaultsCache = next
  writeJson(DEFAULTS_KEY, next)
  listeners.forEach((l) => l())
}

/** Clear all global defaults back to the built-ins. */
export function resetChartDefaults(): void {
  defaultsCache = {}
  removeKey(DEFAULTS_KEY)
  listeners.forEach((l) => l())
}

/** The global chart defaults (built-ins filled in). Re-renders when they change. */
export function useChartDefaults(): ChartOptions {
  const d = useSyncExternalStore(subscribeDefaults, getDefaultsSnapshot)
  return resolveOptions(d, {})
}

/**
 * Chart options for one chart (key = scope + metric name): global defaults plus this chart's overrides,
 * persisted in localStorage. Returns `[options, update, reset, overridden]`; `reset()` drops the chart's
 * overrides so it follows the global defaults again.
 */
export function useChartOptions(
  key: string,
): [ChartOptions, (patch: Partial<ChartOptions>) => void, () => void, boolean] {
  const defaults = useChartDefaults()
  const [overrides, setOverrides] = useState<Partial<ChartOptions>>(() => loadOverrides(key))

  const update = useCallback(
    (patch: Partial<ChartOptions>) => {
      setOverrides((prev) => {
        const next = applyPatch(prev, patch, resolveOptions(getDefaultsSnapshot(), {}))
        saveOverrides(key, next)
        return next
      })
    },
    [key],
  )
  const reset = useCallback(() => {
    saveOverrides(key, {})
    setOverrides({})
  }, [key])

  return [resolveOptions(defaults, overrides), update, reset, Object.keys(overrides).length > 0]
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

/** Format a log-axis tick: plain numbers in the readable range, scientific (1e-3, 2e-5) outside it. */
export function formatLogTick(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return ''
  if (v < 1e-2 || v >= 1e5) return v.toExponential(0).replace('e+', 'e')
  return String(+v.toPrecision(3))
}

export interface LogTicks {
  domain: [number, number]
  ticks: number[]
  /** Faint gridline positions between decades (2..9 x 10^k), only for short axes. */
  minor: number[]
}

const pow10 = (k: number): number => Number(`1e${k}`)

/**
 * Tick layout for a log y-axis over positive data in [min, max]. Axes spanning >= 3 decades snap to whole
 * decades (a tick per decade, thinned past 8 decades, minor lines at 2..9 up to 6 decades). Shorter axes keep
 * the data range and tick at 1/2/5 x 10^k so there are always several labelled values.
 * Returns null when there is no positive finite data.
 */
export function logTicks(min: number, max: number): LogTicks | null {
  if (!(min > 0) || !(max > 0) || !Number.isFinite(min) || !Number.isFinite(max) || max < min) return null
  const lo = Math.floor(Math.log10(min) + 1e-12)
  let hi = Math.ceil(Math.log10(max) - 1e-12)
  if (hi <= lo) hi = lo + 1
  const decades = hi - lo
  if (decades >= 3) {
    const stride = Math.ceil(decades / 8)
    const ticks: number[] = []
    for (let k = lo; k <= hi; k += stride) ticks.push(pow10(k))
    const minor: number[] = []
    if (decades <= 6) for (let k = lo; k < hi; k++) for (let m = 2; m <= 9; m++) minor.push(m * pow10(k))
    return { domain: [pow10(lo), pow10(hi)], ticks, minor }
  }
  const tol = 1 + 1e-9
  const ticks: number[] = []
  for (let k = lo - 1; k <= hi; k++) {
    for (const m of [1, 2, 5]) {
      const v = m * pow10(k)
      if (v * tol >= min && v <= max * tol) ticks.push(v)
    }
  }
  if (ticks.length < 2) {
    // Very narrow range: label the ends so the axis is never blank.
    return { domain: [min, max], ticks: min === max ? [min] : [min, max], minor: [] }
  }
  return { domain: [Math.min(min, ticks[0]), Math.max(max, ticks[ticks.length - 1])], ticks, minor: [] }
}

/** Min/max of the positive finite numbers across chart rows (arrays count elementwise; `x` and `*__n` keys skipped). */
export function positiveRange(rows: Record<string, unknown>[]): [number, number] | null {
  let min = Infinity
  let max = -Infinity
  const see = (v: unknown) => {
    if (typeof v === 'number' && Number.isFinite(v) && v > 0) {
      if (v < min) min = v
      if (v > max) max = v
    }
  }
  for (const r of rows) {
    for (const k of Object.keys(r)) {
      if (k === 'x' || k.endsWith('__n')) continue
      const v = r[k]
      if (Array.isArray(v)) v.forEach(see)
      else see(v)
    }
  }
  return min <= max ? [min, max] : null
}

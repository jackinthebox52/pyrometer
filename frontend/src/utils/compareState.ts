import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { BUILTIN_OPTIONS, MAX_ALPHA, sanitizeOptions } from './chartOptions.ts'
import type { ChartOptions } from './chartOptions.ts'
import type { BandKind, GroupMode } from './grouping.ts'

/**
 * Compare-view settings. They live in the URL query (`#/compare?ids=a,b&group=tag&band=minmax&x=step...`) so a
 * compare view can be bookmarked, shared and navigated with back/forward; localStorage only remembers the
 * last-used values as the fallback for a link that does not say (an old `?ids=` bookmark).
 */
export interface CompareState {
  groupMode: GroupMode
  /** Comma-separated hyperparam keys ignored when grouping by hyperparams. */
  ignoreText: string
  band: BandKind
}

export const DEFAULT_COMPARE_STATE: CompareState = { groupMode: 'none', ignoreText: 'seed', band: 'std' }

const KEY = 'pyrometer.compare'
const STATE_PARAMS = ['group', 'ignore', 'band'] as const
const CHART_PARAMS = ['x', 'log', 'norm', 'smooth'] as const

/** Validate a parsed storage value, falling back to defaults field by field. */
export function sanitizeCompareState(o: unknown): CompareState {
  const out = { ...DEFAULT_COMPARE_STATE }
  if (!o || typeof o !== 'object') return out
  const r = o as Record<string, unknown>
  if (r.groupMode === 'none' || r.groupMode === 'hyperparams' || r.groupMode === 'tag') out.groupMode = r.groupMode
  if (typeof r.ignoreText === 'string') out.ignoreText = r.ignoreText.slice(0, 500)
  if (r.band === 'std' || r.band === 'minmax') out.band = r.band
  return out
}

export function loadCompareState(): CompareState {
  try {
    const raw = localStorage.getItem(KEY)
    return sanitizeCompareState(raw ? JSON.parse(raw) : null)
  } catch {
    return { ...DEFAULT_COMPARE_STATE }
  }
}

export function saveCompareState(s: CompareState): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(s))
  } catch {
    /* storage unavailable: the last-used fallback just does not persist */
  }
}

/** Run ids in `?ids=`: comma separated, empties dropped, duplicates removed. */
export function decodeIds(params: URLSearchParams): string[] {
  return [...new Set((params.get('ids') ?? '').split(',').map((s) => s.trim()).filter(Boolean))]
}

/**
 * Grouping state from the URL. A URL that carries any of `group`/`ignore`/`band` is authoritative (missing
 * fields mean the default, so a shared link reproduces the view); one that carries none uses `fallback`.
 */
export function decodeCompareState(params: URLSearchParams, fallback: CompareState = DEFAULT_COMPARE_STATE): CompareState {
  if (!STATE_PARAMS.some((k) => params.has(k))) return fallback
  const out = { ...DEFAULT_COMPARE_STATE }
  const g = params.get('group')
  if (g === 'none' || g === 'hyperparams' || g === 'tag') out.groupMode = g
  const ignore = params.get('ignore')
  if (ignore != null) out.ignoreText = ignore.slice(0, 500)
  const b = params.get('band')
  if (b === 'std' || b === 'minmax') out.band = b
  return out
}

/** Copy of `params` with the grouping fields set. `group` is always written (it marks the URL as complete). */
export function withCompareState(params: URLSearchParams, s: CompareState): URLSearchParams {
  const out = new URLSearchParams(params)
  for (const k of STATE_PARAMS) out.delete(k)
  out.set('group', s.groupMode)
  if (s.ignoreText !== DEFAULT_COMPARE_STATE.ignoreText) out.set('ignore', s.ignoreText)
  if (s.band !== DEFAULT_COMPARE_STATE.band) out.set('band', s.band)
  return out
}

/** Chart defaults from the URL (`x`, `log`, `norm`, `smooth`), or null when the URL carries none. */
export function decodeChartDefaults(params: URLSearchParams): ChartOptions | null {
  if (!CHART_PARAMS.some((k) => params.has(k))) return null
  const smooth = Number(params.get('smooth'))
  const raw = {
    xAxis: params.get('x') || undefined,
    logY: params.get('log') === '1',
    norm: params.get('norm') ?? undefined,
    alpha: params.has('smooth') && Number.isFinite(smooth) ? smooth : undefined,
  }
  return { ...BUILTIN_OPTIONS, ...sanitizeOptions(raw) }
}

/** Copy of `params` with the chart defaults set (`x` always, the rest only when not the built-in value). */
export function withChartDefaults(params: URLSearchParams, c: ChartOptions): URLSearchParams {
  const out = new URLSearchParams(params)
  for (const k of CHART_PARAMS) out.delete(k)
  out.set('x', c.xAxis)
  if (c.logY) out.set('log', '1')
  if (c.norm !== 'none') out.set('norm', c.norm)
  if (c.alpha > 0) out.set('smooth', String(Math.round(Math.min(MAX_ALPHA, c.alpha) * 100) / 100))
  return out
}

export function sameChartDefaults(a: ChartOptions, b: ChartOptions): boolean {
  return a.xAxis === b.xAxis && a.logY === b.logY && a.norm === b.norm && Math.abs(a.alpha - b.alpha) < 0.005
}

/** Compare-view grouping state, read from the URL (localStorage fallback) and written back to both. */
export function useCompareState(): [CompareState, (patch: Partial<CompareState>, replace?: boolean) => void] {
  const [params, setParams] = useSearchParams()
  const state = useMemo(() => decodeCompareState(params, loadCompareState()), [params])
  const update = useCallback(
    (patch: Partial<CompareState>, replace = false) => {
      const next = { ...state, ...patch }
      saveCompareState(next)
      setParams((prev) => withCompareState(prev, next), { replace })
    },
    [state, setParams],
  )
  return [state, update]
}

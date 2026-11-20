import { useCallback, useState } from 'react'
import type { BandKind, GroupMode } from './grouping.ts'

/**
 * Compare-view settings that should survive reloads. Kept in ONE place on purpose: a later change can move
 * this state into the URL by replacing `loadCompareState` / `saveCompareState` only.
 */
export interface CompareState {
  groupMode: GroupMode
  /** Comma-separated hyperparam keys ignored when grouping by hyperparams. */
  ignoreText: string
  band: BandKind
}

export const DEFAULT_COMPARE_STATE: CompareState = { groupMode: 'none', ignoreText: 'seed', band: 'std' }

const KEY = 'pyrometer.compare'

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
    /* storage unavailable: grouping simply does not persist */
  }
}

/** Compare-view grouping state, persisted. */
export function useCompareState(): [CompareState, (patch: Partial<CompareState>) => void] {
  const [state, setState] = useState<CompareState>(loadCompareState)
  const update = useCallback((patch: Partial<CompareState>) => {
    setState((prev) => {
      const next = { ...prev, ...patch }
      saveCompareState(next)
      return next
    })
  }, [])
  return [state, update]
}

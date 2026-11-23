import { useCallback, useMemo, useSyncExternalStore } from 'react'

/**
 * Per-project view configuration: which tabs, pinned charts and hidden charts a project's run pages show.
 * Stored in localStorage (`pyrometer.view.<project>`), so it applies to every run of the project in this browser.
 *
 * `pinned` / `hidden` entries are metric names or glob-ish patterns (`auto/*`, `*grad*`; `*` matches anything).
 * `pinned === null` means "never customised": the defaults for the run at hand apply (see `defaultPins`).
 */
export interface ViewConfig {
  hiddenTabs: string[]
  pinned: string[] | null
  hidden: string[]
}

export const DEFAULT_VIEW: ViewConfig = { hiddenTabs: [], pinned: null, hidden: [] }

/** Default pins: the first existing name of each group, in order, at most `MAX_DEFAULT_PINS`. */
export const DEFAULT_PIN_GROUPS: readonly (readonly string[])[] = [
  ['auto/loss', 'loss', 'train/loss'],
  ['val/loss', 'eval/loss'],
  ['auto/lr', 'lr'],
  ['auto/tokens_per_sec', 'auto/samples_per_sec', 'auto/steps_per_sec'],
  ['episode/return_mean'],
  ['auto/grad_norm'],
]
export const MAX_DEFAULT_PINS = 4

const PREFIX = 'pyrometer.view.'
const listeners = new Set<() => void>()

export function viewKey(project: string): string {
  return PREFIX + project
}

function globToRegExp(pattern: string): RegExp {
  const src = pattern.split('*').map((s) => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*')
  return new RegExp(`^${src}$`)
}

/** True if `name` equals the pattern or matches it (`*` = any run of characters). */
export function matchesPattern(name: string, pattern: string): boolean {
  return pattern.includes('*') ? globToRegExp(pattern).test(name) : name === pattern
}

/** First existing name of each default group (max 4). */
export function defaultPins(names: readonly string[]): string[] {
  const have = new Set(names)
  const out: string[] = []
  for (const group of DEFAULT_PIN_GROUPS) {
    const hit = group.find((n) => have.has(n))
    if (hit) out.push(hit)
    if (out.length >= MAX_DEFAULT_PINS) break
  }
  return out
}

function strings(v: unknown): string[] | null {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string').slice(0, 200) : null
}

/** Validate a parsed storage value, falling back to defaults field by field. */
export function sanitizeView(o: unknown): ViewConfig {
  if (!o || typeof o !== 'object') return { ...DEFAULT_VIEW }
  const r = o as Record<string, unknown>
  return {
    hiddenTabs: strings(r.hiddenTabs)?.filter((t) => t !== 'overview') ?? [],
    pinned: strings(r.pinned),
    hidden: strings(r.hidden) ?? [],
  }
}

function parse(raw: string | null): ViewConfig {
  if (!raw) return { ...DEFAULT_VIEW }
  try {
    return sanitizeView(JSON.parse(raw))
  } catch {
    return { ...DEFAULT_VIEW }
  }
}

function readRaw(project: string): string | null {
  try {
    return localStorage.getItem(viewKey(project))
  } catch {
    return null
  }
}

export function loadView(project: string): ViewConfig {
  return parse(readRaw(project))
}

export function saveView(project: string, cfg: ViewConfig): void {
  try {
    localStorage.setItem(viewKey(project), JSON.stringify(cfg))
  } catch {
    /* storage unavailable: the view just does not persist */
  }
  listeners.forEach((l) => l())
}

export function resetView(project: string): void {
  try {
    localStorage.removeItem(viewKey(project))
  } catch {
    /* ignore */
  }
  listeners.forEach((l) => l())
}

export function isHidden(name: string, cfg: ViewConfig): boolean {
  return cfg.hidden.some((p) => matchesPattern(name, p))
}

/** Pinned by an explicit entry, or (never customised) by the defaults for `available`. */
export function isPinned(name: string, cfg: ViewConfig, available: readonly string[] = []): boolean {
  const list = cfg.pinned ?? defaultPins(available)
  return list.some((p) => matchesPattern(name, p))
}

/** Pinned metric names present in `names`, in pin order (patterns expand in name order), hidden ones dropped. */
export function resolvePins(cfg: ViewConfig, names: readonly string[]): string[] {
  const out: string[] = []
  for (const p of cfg.pinned ?? defaultPins(names)) {
    for (const n of names) if (matchesPattern(n, p) && !out.includes(n) && !isHidden(n, cfg)) out.push(n)
  }
  return out
}

/**
 * Pin or unpin `name`. A never-customised config first materialises the default pins. Unpinning a name that only a
 * glob covers expands the glob into the other names it matches (a glob cannot exclude one name).
 */
export function togglePin(cfg: ViewConfig, name: string, names: readonly string[]): ViewConfig {
  const list = cfg.pinned ?? defaultPins(names)
  if (!list.some((p) => matchesPattern(name, p))) return { ...cfg, pinned: [...list, name] }
  const next = list.flatMap((p) =>
    !matchesPattern(name, p) ? [p] : p.includes('*') ? names.filter((n) => n !== name && matchesPattern(n, p)) : [],
  )
  return { ...cfg, pinned: [...new Set(next)] }
}

export function removePin(cfg: ViewConfig, entry: string, names: readonly string[]): ViewConfig {
  return { ...cfg, pinned: (cfg.pinned ?? defaultPins(names)).filter((p) => p !== entry) }
}

/** Move the pin at `index` by `dir` (-1 up, +1 down). Out-of-range moves are no-ops. */
export function movePin(list: readonly string[], index: number, dir: -1 | 1): string[] {
  const j = index + dir
  if (index < 0 || index >= list.length || j < 0 || j >= list.length) return [...list]
  const out = [...list]
  ;[out[index], out[j]] = [out[j], out[index]]
  return out
}

export function hidePattern(cfg: ViewConfig, pattern: string): ViewConfig {
  return cfg.hidden.includes(pattern) ? cfg : { ...cfg, hidden: [...cfg.hidden, pattern] }
}

export function showPattern(cfg: ViewConfig, pattern: string): ViewConfig {
  return { ...cfg, hidden: cfg.hidden.filter((p) => p !== pattern) }
}

export function setTabHidden(cfg: ViewConfig, tab: string, hide: boolean): ViewConfig {
  if (tab === 'overview') return cfg
  const rest = cfg.hiddenTabs.filter((t) => t !== tab)
  return { ...cfg, hiddenTabs: hide ? [...rest, tab] : rest }
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  const onStorage = (e: StorageEvent) => {
    if (e.key === null || e.key.startsWith(PREFIX)) cb()
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(cb)
    window.removeEventListener('storage', onStorage)
  }
}

/** The project's view config; re-renders on any change (this tab, or another tab via the `storage` event). */
export function useViewConfig(project: string): {
  cfg: ViewConfig
  update: (fn: (cfg: ViewConfig) => ViewConfig) => void
  reset: () => void
} {
  const raw = useSyncExternalStore(
    subscribe,
    () => readRaw(project),
    () => null,
  )
  const cfg = useMemo(() => parse(raw), [raw])
  const update = useCallback((fn: (c: ViewConfig) => ViewConfig) => saveView(project, fn(loadView(project))), [project])
  const reset = useCallback(() => resetView(project), [project])
  return { cfg, update, reset }
}

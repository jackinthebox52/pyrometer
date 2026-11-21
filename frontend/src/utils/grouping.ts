import type { Run } from '../api/types.ts'

export type GroupMode = 'none' | 'hyperparams' | 'tag'
export type BandKind = 'std' | 'minmax'

export interface RunGroup {
  id: string
  label: string
  runIds: string[]
}

/** Parse "seed, run_idx" into a set of hyperparam keys to ignore. */
export function parseIgnoreKeys(text: string): Set<string> {
  return new Set(
    text
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
  )
}

function stable(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stable).join(',')}]`
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>
    return `{${Object.keys(o)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${stable(o[k])}`)
      .join(',')}}`
  }
  return JSON.stringify(v) ?? 'null'
}

function show(v: unknown): string {
  return typeof v === 'string' ? v : stable(v)
}

/**
 * Group runs. 'hyperparams': runs whose hyperparams are equal after dropping `ignore` keys (e.g. seed).
 * 'tag': runs with the same set of tags after dropping tags shared by every run. Group order follows
 * the first run of each group. Labels list the keys/tags that distinguish groups from each other.
 */
export function groupRuns(runs: Run[], mode: GroupMode, ignore: Set<string>): RunGroup[] {
  if (mode === 'none') return runs.map((r) => ({ id: `g:${r.run_id}`, label: r.name, runIds: [r.run_id] }))
  const buckets = new Map<string, Run[]>()
  const keyOf = (r: Run): string => {
    if (mode === 'tag') {
      const common = runs.length > 0 ? runs[0].tags.filter((t) => runs.every((o) => o.tags.includes(t))) : []
      return [...r.tags].filter((t) => !common.includes(t)).sort().join('|')
    }
    const hp: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(r.hyperparams ?? {})) if (!ignore.has(k)) hp[k] = v
    return stable(hp)
  }
  for (const r of runs) {
    const k = keyOf(r)
    const arr = buckets.get(k)
    if (arr) arr.push(r)
    else buckets.set(k, [r])
  }
  const groups = [...buckets.entries()]
  const label = (members: Run[], idx: number): string => {
    const first = members[0]
    if (mode === 'tag') {
      const k = keyOf(first)
      return k ? k.split('|').join(', ') : 'untagged'
    }
    const keys = new Set<string>()
    for (const [, a] of groups) for (const k of Object.keys(a[0].hyperparams ?? {})) keys.add(k)
    const differing = [...keys].filter((k) => {
      if (ignore.has(k)) return false
      const vals = new Set(groups.map(([, a]) => stable(a[0].hyperparams?.[k])))
      return vals.size > 1
    })
    if (differing.length === 0) return groups.length === 1 ? 'all runs' : `group ${idx + 1}`
    return differing.map((k) => `${k}=${show(first.hyperparams?.[k])}`).join(' ')
  }
  return groups.map(([, members], i) => ({
    id: `g${i}`,
    label: label(members, i),
    runIds: members.map((m) => m.run_id),
  }))
}

export interface XY {
  x: number
  y: number
}

export interface AggPoint {
  x: number
  mean: number
  lo: number
  hi: number
  n: number
}

/** Linear interpolation on a series sorted by x; null outside its span. */
function interp(pts: XY[], x: number): number | null {
  if (pts.length === 0 || x < pts[0].x || x > pts[pts.length - 1].x) return null
  let lo = 0
  let hi = pts.length - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (pts[mid].x <= x) lo = mid
    else hi = mid
  }
  const a = pts[lo]
  const b = pts[hi]
  if (b.x === a.x || x <= a.x) return a.y
  if (x >= b.x) return b.y
  return a.y + ((b.y - a.y) * (x - a.x)) / (b.x - a.x)
}

const MAX_GRID = 600

/**
 * Aggregate several runs' series (each sorted by x, finite values only) onto a common x grid (union of all
 * x values, thinned to MAX_GRID). A grid point is only emitted where at least 2 runs cover it
 * (interpolated linearly; no extrapolation). Bands are mean +/- sample std, or min/max. A std band is clamped to the
 * range the runs actually reached, so a positive metric (loss, throughput) never draws a band below zero.
 */
export function aggregate(runs: XY[][], band: BandKind): AggPoint[] {
  const nonEmpty = runs.filter((r) => r.length > 0)
  if (nonEmpty.length < 2) return []
  let grid = [...new Set(nonEmpty.flatMap((r) => r.map((p) => p.x)))].sort((a, b) => a - b)
  if (grid.length > MAX_GRID) {
    const stride = grid.length / MAX_GRID
    grid = Array.from({ length: MAX_GRID }, (_, i) => grid[Math.floor(i * stride)])
  }
  let gMin = Infinity
  let gMax = -Infinity
  for (const r of nonEmpty) {
    for (const p of r) {
      if (p.y < gMin) gMin = p.y
      if (p.y > gMax) gMax = p.y
    }
  }
  const out: AggPoint[] = []
  for (const x of grid) {
    const vals: number[] = []
    for (const r of nonEmpty) {
      const v = interp(r, x)
      if (v != null) vals.push(v)
    }
    const n = vals.length
    if (n < 2) continue
    const mean = vals.reduce((s, v) => s + v, 0) / n
    if (band === 'minmax') {
      out.push({ x, mean, lo: Math.min(...vals), hi: Math.max(...vals), n })
    } else {
      const std = Math.sqrt(vals.reduce((s, v) => s + (v - mean) ** 2, 0) / (n - 1))
      out.push({ x, mean, lo: Math.max(mean - std, gMin), hi: Math.min(mean + std, gMax), n })
    }
  }
  return out
}

import type { ParamStat } from '../api/types.ts'

export interface HeatmapBins {
  /** First step of each column, ascending. */
  steps: number[]
  /** Last step covered by each column (equals `steps[i]` when a column holds one step). */
  ends: number[]
  /** `${param}::${columnIndex}` -> largest grad_norm of that parameter within the column. */
  cells: Map<string, number>
}

/**
 * Put every parameter on one shared column grid. The server downsamples each parameter on its own, so their
 * kept steps differ; binning by step (and keeping the max, as the server's peak buckets do) makes the
 * columns line up whatever the resolution. At most `maxCols` columns; steps are grouped in equal step widths.
 */
export function binHeatmap(stats: ParamStat[], maxCols: number): HeatmapBins {
  const steps = [...new Set(stats.map((s) => s.step))].sort((a, b) => a - b)
  const cells = new Map<string, number>()
  if (steps.length === 0) return { steps: [], ends: [], cells }
  const cols = Math.max(1, Math.floor(maxCols))
  if (steps.length <= cols) {
    for (const s of stats) cells.set(`${s.param_name}::${steps.indexOf(s.step)}`, s.grad_norm)
    return { steps, ends: steps, cells }
  }
  const lo = steps[0]
  const width = (steps[steps.length - 1] - lo + 1) / cols
  const binOf = (step: number) => Math.min(cols - 1, Math.floor((step - lo) / width))
  const first = new Map<number, number>()
  const last = new Map<number, number>()
  for (const st of steps) {
    const b = binOf(st)
    if (!first.has(b)) first.set(b, st)
    last.set(b, st)
  }
  const used = [...first.keys()].sort((a, b) => a - b)
  const col = new Map(used.map((b, i) => [b, i]))
  for (const s of stats) {
    const key = `${s.param_name}::${col.get(binOf(s.step))}`
    const prev = cells.get(key)
    if (prev === undefined || s.grad_norm > prev) cells.set(key, s.grad_norm)
  }
  return { steps: used.map((b) => first.get(b)!), ends: used.map((b) => last.get(b)!), cells }
}

import type { XAxis, XPoint } from './xAxis.ts'
import { axisMetricName } from './xAxis.ts'

/** A zoom selection in the chart's own x domain (what the user dragged over). */
export interface XZoom {
  axis: XAxis
  x0: number
  x1: number
}

/** The server query a zoom turns into: a step window or a timestamp window (Unix seconds). */
export type RangeQuery =
  | { step_min: number; step_max: number }
  | { t_min: number; t_max: number }

export const MIN_POINTS = 200
export const MAX_POINTS = 4000

/** Points to ask the server for: about two per pixel (min/max buckets need that), within sane bounds. */
export function maxPointsForWidth(widthPx: number): number {
  if (!Number.isFinite(widthPx) || widthPx <= 0) return 1000
  return Math.min(MAX_POINTS, Math.max(MIN_POINTS, Math.round(widthPx * 2)))
}

/**
 * Turn a drag selection into a server query for one series.
 *  - step axis: the step window itself;
 *  - time / rel axes: a timestamp window ('rel' adds the run start);
 *  - metric axes: the steps of this series' points whose axis value is inside the selection, widened by one
 *    neighbouring point on each side (so the zoomed line still reaches the selection edges).
 * Returns null for an empty/degenerate selection (nothing to zoom to).
 */
export function zoomQuery(zoom: XZoom, points: XPoint[], startTime = 0): RangeQuery | null {
  const lo = Math.min(zoom.x0, zoom.x1)
  const hi = Math.max(zoom.x0, zoom.x1)
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return null
  if (axisMetricName(zoom.axis) != null) {
    let first = -1
    let last = -1
    points.forEach((p, i) => {
      if (p.x >= lo && p.x <= hi) {
        if (first < 0) first = i
        last = i
      }
    })
    if (first < 0) return null
    const a = points[Math.max(first - 1, 0)]
    const b = points[Math.min(last + 1, points.length - 1)]
    const steps = [a.step, b.step, points[first].step, points[last].step]
    return { step_min: Math.min(...steps), step_max: Math.max(...steps) }
  }
  if (zoom.axis === 'time') return { t_min: lo, t_max: hi }
  if (zoom.axis === 'rel') return { t_min: startTime + lo, t_max: startTime + hi }
  return { step_min: Math.floor(lo), step_max: Math.ceil(hi) }
}

/**
 * Merge freshly fetched rows into an existing (possibly downsampled) series. Rows are keyed by `keyOf`; an
 * incoming row replaces the stored one (last write wins on the server too) and the result is sorted by `order`.
 * Used for live updates: only the new tail is fetched and folded in instead of refetching everything.
 */
export function mergeBuckets<T>(existing: T[], incoming: T[], keyOf: (r: T) => string, order: (a: T, b: T) => number): T[] {
  if (incoming.length === 0) return existing
  const byKey = new Map<string, T>()
  for (const r of existing) byKey.set(keyOf(r), r)
  for (const r of incoming) byKey.set(keyOf(r), r)
  return [...byKey.values()].sort(order)
}

/** Live tail merges add full-resolution points; once the held rows far exceed the budget, resample from the server. */
export function needsResample(rows: number, seriesCount: number, maxPoints: number): boolean {
  return rows > Math.max(1, seriesCount) * maxPoints * 2
}

/** Start of the tail window for a live refetch: a little before the newest timestamp already held. */
export function tailStart(rows: { timestamp: number }[], marginSeconds = 5): number | undefined {
  if (rows.length === 0) return undefined
  let newest = -Infinity
  for (const r of rows) if (r.timestamp > newest) newest = r.timestamp
  return newest - marginSeconds
}

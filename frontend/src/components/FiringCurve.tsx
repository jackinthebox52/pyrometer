import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import type { RunEvent, SystemSnapshot } from '../api/types.ts'
import { C, N, RED, heatColor } from '../theme.ts'

/**
 * The firing curve: the run's GPU utilisation over its whole life, drawn as an area of thin vertical slices, each
 * slice coloured by its own value on the blackbody ramp (colour by value, not by position), so hot periods glow and
 * idle ones stay dull red. Event marks ("bent cones") sit above it; anomalies are red.
 *
 * Interaction (the signature): hover shows a hairline and a readout (elapsed, step, GPU %, nearest event). A drag
 * selects a time range and a plain click selects a window of +-2.5% of the run around the click point; both call
 * `onSelectRange(t_min, t_max)` with Unix seconds. The run page turns that into a timestamp window (`t_min/t_max`
 * is a query the metrics endpoint already supports, and every metric row carries its timestamp), switches to the
 * Metrics tab and refetches every series for that window at full resolution. Time -> step is therefore not needed
 * to zoom; `stepAt` is only used to label the readout.
 */

const COLUMNS = 300
const H = 110
const CONE_BAND = 14
const AXIS_H = 18
const PLOT_TOP = CONE_BAND
const PLOT_H = H - CONE_BAND - AXIS_H
const DRAG_PX = 4
const CLICK_WINDOW = 0.025

export interface FiringCurveProps {
  /** System snapshots of the run (any order, any number of GPUs). */
  snapshots: SystemSnapshot[]
  events: RunEvent[]
  /** Run start, Unix seconds; elapsed time is measured from here. */
  startTime: number
  running: boolean
  /** Step in effect at a timestamp, if known (labels the readout). */
  stepAt?: (t: number) => number | null
  onSelectRange?: (tMin: number, tMax: number) => void
}

interface Column {
  t: number
  util: number | null
}

function utilOf(s: SystemSnapshot): number | null {
  return s.attributed_utilization ?? s.gpu_utilization
}

/** Bucket into COLUMNS slices: mean per GPU in the slice, then mean across GPUs. Short gaps carry the last value. */
export function buildColumns(snaps: SystemSnapshot[], t0: number, t1: number, n = COLUMNS): Column[] {
  const span = Math.max(t1 - t0, 1e-6)
  const sums = Array.from({ length: n }, () => new Map<number, { s: number; c: number }>())
  for (const s of snaps) {
    const u = utilOf(s)
    if (u == null || !Number.isFinite(u)) continue
    const i = Math.min(n - 1, Math.max(0, Math.floor(((s.timestamp - t0) / span) * n)))
    const m = sums[i]
    const g = s.gpu_index ?? 0
    const e = m.get(g) ?? { s: 0, c: 0 }
    e.s += u
    e.c += 1
    m.set(g, e)
  }
  const cols: Column[] = sums.map((m, i) => {
    const per = [...m.values()].map((e) => e.s / e.c)
    return { t: t0 + ((i + 0.5) / n) * span, util: per.length ? per.reduce((a, b) => a + b, 0) / per.length : null }
  })
  // Fill gaps of up to 3 empty slices (downsampled or sparse series) with the last known value; longer gaps stay blank.
  let last: number | null = null
  let gap = 0
  for (const c of cols) {
    if (c.util != null) {
      last = c.util
      gap = 0
    } else if (last != null && ++gap <= 3) c.util = last
  }
  return cols
}

function fmtElapsed(secs: number): string {
  const s = Math.round(Math.max(0, secs))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m > 0) return `${m}m ${String(r).padStart(2, '0')}s`
  return `${r}s`
}

function niceTicks(span: number, count: number): number[] {
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400]
  const target = span / count
  const step = steps.find((s) => s >= target) ?? steps[steps.length - 1]
  const out: number[] = []
  for (let t = 0; t <= span; t += step) out.push(t)
  return out
}

/** Compact axis label: 30s, 2m 30s, 15m, 1h 15m, 2h. */
export function fmtTick(secs: number): string {
  const s = Math.round(Math.max(0, secs))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  if (h > 0) return m ? `${h}h ${m}m` : `${h}h`
  if (m > 0) return r ? `${m}m ${r}s` : `${m}m`
  return `${r}s`
}

/** Util 0-100 to a ramp colour: idle is dull red, busy is rich amber-yellow; white only from 98%. */
export function utilColor(u: number): string {
  const x = Math.min(100, Math.max(0, u))
  return heatColor(x >= 98 ? 0.97 : 0.08 + 0.8 * (x / 100))
}

interface Run {
  start: number
  end: number
  smooth: number[]
  raw: number[]
}

/** Contiguous runs of slices with a sample (gaps stay gaps), with a 3-slice moving average for the top edge. */
export function contiguousRuns(cols: Column[]): Run[] {
  const runs: Run[] = []
  let i = 0
  while (i < cols.length) {
    if (cols[i].util == null) {
      i++
      continue
    }
    const start = i
    const raw: number[] = []
    while (i < cols.length && cols[i].util != null) raw.push(cols[i++].util as number)
    const smooth = raw.map((_, k) => {
      const a = Math.max(0, k - 1)
      const b = Math.min(raw.length - 1, k + 1)
      let sum = 0
      for (let j = a; j <= b; j++) sum += raw[j]
      return sum / (b - a + 1)
    })
    runs.push({ start, end: i - 1, smooth, raw })
  }
  return runs
}

const isAnomaly = (e: RunEvent) => e.event_type === 'anomaly_detected'

export default function FiringCurve({ snapshots, events, startTime, running, stepAt, onSelectRange }: FiringCurveProps) {
  const boxRef = useRef<HTMLDivElement>(null)
  const gradId = useId()
  const [width, setWidth] = useState(0)
  const [hoverX, setHoverX] = useState<number | null>(null)
  const [drag, setDrag] = useState<{ x0: number; x1: number } | null>(null)
  const dragRef = useRef<{ x0: number; moved: boolean } | null>(null)

  useEffect(() => {
    const el = boxRef.current
    if (!el) return
    const read = () => setWidth(el.clientWidth)
    read()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(read)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const { t0, t1, cols, mean, current } = useMemo(() => {
    const withUtil = snapshots.filter((s) => utilOf(s) != null)
    if (withUtil.length === 0) return { t0: 0, t1: 0, cols: [] as Column[], mean: null, current: null }
    let lo = Infinity
    let hi = -Infinity
    for (const s of withUtil) {
      if (s.timestamp < lo) lo = s.timestamp
      if (s.timestamp > hi) hi = s.timestamp
    }
    const c = buildColumns(withUtil, lo, hi)
    const known = c.filter((x) => x.util != null) as { t: number; util: number }[]
    const m = known.length ? known.reduce((a, x) => a + x.util, 0) / known.length : null
    const lastCol = [...c].reverse().find((x) => x.util != null)
    return { t0: lo, t1: hi > lo ? hi : lo + 1, cols: c, mean: m, current: lastCol?.util ?? null }
  }, [snapshots])

  const span = t1 - t0
  // Elapsed time counts from run start (snapshots normally begin a little after it); never later than the first sample.
  const origin = Math.min(startTime, t0)
  const visibleEvents = useMemo(
    () => events.filter((e) => e.timestamp >= t0 - span * 0.01 && e.timestamp <= t1 + span * 0.01),
    [events, t0, t1, span],
  )

  const xOf = (t: number) => ((t - t0) / span) * width
  const tOf = (x: number) => t0 + (Math.min(Math.max(x, 0), width) / width) * span

  const empty = cols.length === 0
  const hoverT = hoverX != null && !empty ? tOf(hoverX) : null
  const hoverCol = hoverT != null ? cols[Math.min(cols.length - 1, Math.floor(((hoverT - t0) / span) * cols.length))] : null

  const nearestEvent = useMemo(() => {
    if (hoverX == null || width === 0) return null
    let best: RunEvent | null = null
    let bd = 10
    for (const e of visibleEvents) {
      const d = Math.abs(((e.timestamp - t0) / span) * width - hoverX)
      if (d <= bd) {
        bd = d
        best = e
      }
    }
    return best
  }, [hoverX, visibleEvents, t0, span, width])

  function localX(e: ReactPointerEvent): number {
    const r = e.currentTarget.getBoundingClientRect()
    return e.clientX - r.left
  }

  function onDown(e: ReactPointerEvent<SVGRectElement>) {
    if (empty || !onSelectRange) return
    e.currentTarget.setPointerCapture?.(e.pointerId)
    const x = localX(e)
    dragRef.current = { x0: x, moved: false }
    setDrag({ x0: x, x1: x })
  }
  function onMove(e: ReactPointerEvent<SVGRectElement>) {
    const x = localX(e)
    setHoverX(x)
    const d = dragRef.current
    if (!d) return
    if (Math.abs(x - d.x0) > DRAG_PX) d.moved = true
    setDrag({ x0: d.x0, x1: x })
  }
  function onUp(e: ReactPointerEvent<SVGRectElement>) {
    const d = dragRef.current
    dragRef.current = null
    setDrag(null)
    if (!d || !onSelectRange) return
    const x = localX(e)
    if (d.moved) {
      const a = tOf(Math.min(d.x0, x))
      const b = tOf(Math.max(d.x0, x))
      if (b > a) onSelectRange(a, b)
    } else {
      const c = tOf(x)
      onSelectRange(Math.max(t0, c - span * CLICK_WINDOW), Math.min(t1, c + span * CLICK_WINDOW))
    }
  }

  const slice = width / COLUMNS
  const runs = useMemo(() => contiguousRuns(cols), [cols])
  const ticks = width > 0 && !empty ? niceTicks(t1 - origin, Math.max(2, Math.floor(width / 110))).filter((e) => origin + e >= t0 && origin + e <= t1) : []

  const readoutLeft = hoverX != null && width > 0 && hoverX > width * 0.6
  const step = hoverT != null ? stepAt?.(hoverT) ?? null : null

  return (
    <section aria-label="GPU load over the run" className="border border-py-border bg-py-surface rounded px-3 pt-2.5 pb-1.5">
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-[13px] font-semibold text-py-text">GPU load</h2>
        {!empty && (
          <div className="num text-[11px] text-py-muted">
            {mean != null && <span>mean {mean.toFixed(0)}%</span>}
            {running && current != null && <span className="ml-3">now <span className="text-py-text">{current.toFixed(0)}%</span></span>}
          </div>
        )}
      </div>

      {empty ? (
        <div className="flex items-center text-xs text-py-muted" style={{ height: H - 22 }}>
          {running
            ? 'No GPU samples. They appear here every 2 s once the run reports an NVIDIA GPU.'
            : 'No GPU samples were recorded for this run.'}
        </div>
      ) : (
        <div ref={boxRef} className="relative w-full select-none" style={{ height: H }}>
          {width > 0 && (
            <svg width={width} height={H} role="img" aria-label={`GPU utilisation, mean ${mean?.toFixed(0) ?? 'unknown'} percent`} className="block overflow-visible">
              <line x1={0} x2={width} y1={PLOT_TOP + PLOT_H} y2={PLOT_TOP + PLOT_H} stroke={N.n5} strokeWidth={1} />
              <line x1={0} x2={width} y1={PLOT_TOP + PLOT_H / 2} y2={PLOT_TOP + PLOT_H / 2} stroke={N.n4} strokeWidth={1} strokeDasharray="2 4" />
              {runs.map((r, k) => {
                const x0 = r.start * slice
                const x1 = (r.end + 1) * slice
                const yOf = (u: number) => PLOT_TOP + PLOT_H - Math.max(1, (Math.min(100, Math.max(0, u)) / 100) * PLOT_H)
                const pts = r.smooth.map((u, j) => `${((r.start + j + 0.5) * slice).toFixed(2)} ${yOf(u).toFixed(2)}`)
                const base = PLOT_TOP + PLOT_H
                const d = `M${x0.toFixed(2)} ${base}L${x0.toFixed(2)} ${yOf(r.smooth[0]).toFixed(2)}L${pts.join('L')}L${x1.toFixed(2)} ${yOf(r.smooth[r.smooth.length - 1]).toFixed(2)}L${x1.toFixed(2)} ${base}Z`
                const id = `${gradId}-${k}`
                return (
                  <g key={k}>
                    <defs>
                      <linearGradient id={id} gradientUnits="userSpaceOnUse" x1={x0} x2={Math.max(x1, x0 + 1)} y1={0} y2={0}>
                        {r.raw.map((u, j) => (
                          <stop key={j} offset={`${(((j + 0.5) / r.raw.length) * 100).toFixed(2)}%`} stopColor={utilColor(u)} />
                        ))}
                      </linearGradient>
                    </defs>
                    <path d={d} fill={`url(#${id})`} />
                  </g>
                )
              })}

              {ticks.map((e) => {
                const x = xOf(origin + e)
                return (
                  <g key={e}>
                    <line x1={x} x2={x} y1={PLOT_TOP + PLOT_H} y2={PLOT_TOP + PLOT_H + 3} stroke={N.n5} />
                    <text x={x} y={H - 4} textAnchor={x < 14 ? 'start' : x > width - 24 ? 'end' : 'middle'} className="num" fontSize={10} fill={C.faint}>
                      {fmtTick(e)}
                    </text>
                  </g>
                )
              })}

              {visibleEvents.map((ev, i) => {
                const x = xOf(ev.timestamp)
                return (
                  // A 10px triangle pointing at the moment, tilted 15 degrees.
                  <g key={i} transform={`translate(${x} ${CONE_BAND - 2}) rotate(15)`}>
                    <title>{`${ev.event_type}: ${ev.message}`}</title>
                    <polygon points="-5,-10 5,-10 0,0" fill={isAnomaly(ev) ? RED : C.muted} />
                  </g>
                )
              })}

              {drag && <rect x={Math.min(drag.x0, drag.x1)} y={PLOT_TOP} width={Math.abs(drag.x1 - drag.x0)} height={PLOT_H} fill={C.text} fillOpacity={0.14} stroke={C.soft} strokeWidth={1} />}
              {hoverX != null && !drag && <line x1={hoverX} x2={hoverX} y1={0} y2={PLOT_TOP + PLOT_H} stroke={C.text} strokeOpacity={0.7} strokeWidth={1} />}

              <rect
                x={0}
                y={0}
                width={width}
                height={PLOT_TOP + PLOT_H}
                fill="transparent"
                style={{ cursor: onSelectRange ? 'crosshair' : 'default', touchAction: 'pan-y' }}
                onPointerDown={onDown}
                onPointerMove={onMove}
                onPointerUp={onUp}
                onPointerLeave={() => !dragRef.current && setHoverX(null)}
                onPointerCancel={() => {
                  dragRef.current = null
                  setDrag(null)
                }}
              />
            </svg>
          )}
          {hoverX != null && hoverT != null && hoverCol && !drag && (
            <div
              className="pointer-events-none absolute z-10 top-4 max-w-[280px] rounded border border-py-border-hi bg-py-surface px-2 py-1 text-[11px] text-py-soft"
              style={readoutLeft ? { right: width - hoverX + 8 } : { left: hoverX + 8 }}
            >
              <div className="num text-py-text">
                {fmtElapsed(hoverT - origin)}
                {step != null && <span className="text-py-muted"> · step {step.toLocaleString()}</span>}
              </div>
              <div className="num">GPU {hoverCol.util != null ? `${hoverCol.util.toFixed(0)}%` : 'no sample'}</div>
              {nearestEvent && (
                <div className={`mt-0.5 break-words ${isAnomaly(nearestEvent) ? 'text-py-red' : 'text-py-muted'}`}>{nearestEvent.message}</div>
              )}
            </div>
          )}
        </div>
      )}
      {!empty && onSelectRange && <p className="pb-0.5 text-[11px] text-py-faint">Drag to open that period in Metrics.</p>}
    </section>
  )
}

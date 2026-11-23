import * as d3 from 'd3'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { ParamStat } from '../api/types.ts'
import { api } from '../api/client.ts'
import type { Metric } from '../api/types.ts'
import { binHeatmap, columnAt, columnsToStepRange } from '../utils/heatmap.ts'
import { useZoomFetch } from '../hooks/useZoomFetch.ts'
import type { XZoom } from '../utils/series.ts'
import type { XSource } from '../utils/xAxis.ts'
import type { RangeQuery } from '../utils/series.ts'
import { HEAT, HEAT_GRADIENT, N, STEEL, heatColor } from '../theme.ts'
import Icon from './Icon.tsx'
import MetricChart from './MetricChart.tsx'

interface Props {
  stats: ParamStat[]
  /** With a run id the detail charts of a selected parameter can be zoomed (refetched at full resolution). */
  runId?: string
  /** Bump when new stats arrive so zoomed windows refresh. */
  refreshKey?: number
}

/** Gradient/weight detail charts offer step, wall time and elapsed on the x axis (no metric axes). */
const DETAIL_SOURCE: XSource = {}

const NAME_COL_W = 76
const ROW_H = 13
const TOP_AXIS_H = 28
const LEFT_PAD = 4
const CELL_GAP = 1
const MIN_CELL_W = 3

export default function GradientHeatmap({ stats: allStats, runId, refreshKey }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const [containerWidth, setContainerWidth] = useState(0)
  const [selectedParam, setSelectedParam] = useState<string | null>(null)

  // Drag across columns to zoom: every parameter is refetched for that step window at full resolution.
  const maxCols = Math.max(1, Math.floor((containerWidth - NAME_COL_W - LEFT_PAD) / (MIN_CELL_W + CELL_GAP)))
  const maxColsRef = useRef(maxCols)
  maxColsRef.current = maxCols
  const zoomFetch = useZoomFetch<ParamStat[]>(
    runId
      ? async (z: XZoom) => {
          const r = await api.getParamStatsSeries(runId, {
            step_min: Math.floor(z.x0),
            step_max: Math.ceil(z.x1),
            max_points: maxColsRef.current,
          })
          return r.data.length > 0 ? r.data : null
        }
      : undefined,
    refreshKey,
  )
  const stats = zoomFetch.data ?? allStats
  const layout = useRef({ cellW: MIN_CELL_W, steps: [] as number[], ends: [] as number[] })
  const dragRef = useRef<{ a: number; b: number } | null>(null)
  const justDragged = useRef(false)
  const [sel, setSel] = useState<[number, number] | null>(null)
  const colFromEvent = (e: React.MouseEvent): number | null => {
    const svg = svgRef.current
    if (!svg) return null
    const x = e.clientX - svg.getBoundingClientRect().left - NAME_COL_W - LEFT_PAD
    if (x < 0) return null
    return columnAt(x, layout.current.steps.length, layout.current.cellW + CELL_GAP)
  }
  const dragHandlers = runId
    ? {
        onMouseDown: (e: React.MouseEvent) => {
          const c = colFromEvent(e)
          if (c == null || e.button !== 0) return
          dragRef.current = { a: c, b: c }
        },
        onMouseMove: (e: React.MouseEvent) => {
          const d = dragRef.current
          const c = colFromEvent(e)
          if (!d || c == null) return
          d.b = c
          if (d.a !== d.b) setSel([Math.min(d.a, d.b), Math.max(d.a, d.b)])
        },
        onMouseUp: () => {
          const d = dragRef.current
          dragRef.current = null
          setSel(null)
          if (!d || d.a === d.b) return
          justDragged.current = true // the click that follows a drag must not select a parameter
          setTimeout(() => (justDragged.current = false), 0)
          const r = columnsToStepRange(layout.current, d.a, d.b)
          if (r) zoomFetch.select({ axis: 'step', x0: r.step_min, x1: r.step_max })
        },
        onMouseLeave: () => {
          dragRef.current = null
          setSel(null)
        },
      }
    : {}
  const selBox = useMemo(
    () =>
      sel
        ? { left: NAME_COL_W + LEFT_PAD + sel[0] * (layout.current.cellW + CELL_GAP), width: (sel[1] - sel[0] + 1) * (layout.current.cellW + CELL_GAP) }
        : null,
    [sel],
  )

  // Track container width
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    setContainerWidth(el.clientWidth)
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width
      if (w) setContainerWidth(w)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // d3 draw — all derived data computed here to avoid stale-closure issues
  useEffect(() => {
    const svg = svgRef.current
    if (!svg || !containerWidth || stats.length === 0) return

    // Build param order (insertion order from the data, preserving model layer order)
    const paramNames: string[] = []
    const seenParams = new Set<string>()
    for (const s of stats) {
      if (!seenParams.has(s.param_name)) {
        seenParams.add(s.param_name)
        paramNames.push(s.param_name)
      }
    }

    // Shared column grid, at most one column per MIN_CELL_W pixels (parameters are downsampled separately
    // by the server, so their steps differ; binning aligns them).
    const { steps, ends, cells: matrix } = binHeatmap(stats, maxCols)

    if (paramNames.length === 0 || steps.length === 0) return

    // Log-scale normalization
    const allNorms = stats.map((s) => s.grad_norm).filter((v) => v > 0)
    const logMin = allNorms.length > 0 ? Math.log10(Math.max(d3.min(allNorms)!, 1e-10)) : -8
    const logMax = allNorms.length > 0 ? Math.log10(Math.max(d3.max(allNorms)!, 1e-10)) : 0
    const logRange = logMax - logMin || 1

    function cellColor(norm: number): string {
      if (!norm || norm <= 0) return N.n3
      const logVal = (Math.log10(Math.max(norm, 1e-10)) - logMin) / logRange
      return heatColor(Math.max(0, Math.min(1, logVal)))
    }

    const availW = containerWidth - NAME_COL_W - LEFT_PAD
    const cellW = Math.max(MIN_CELL_W, availW / steps.length - CELL_GAP)
    layout.current = { cellW, steps, ends }
    const totalW = Math.max(containerWidth, NAME_COL_W + LEFT_PAD + steps.length * (cellW + CELL_GAP))
    const totalH = TOP_AXIS_H + paramNames.length * ROW_H

    d3.select(svg).selectAll('*').remove()
    d3.select(svg).attr('width', totalW).attr('height', totalH)

    const root = d3.select(svg).append('g')

    // X-axis: step labels (up to ~12 evenly spaced)
    const labelEvery = Math.max(1, Math.ceil(steps.length / 12))
    root
      .append('g')
      .selectAll<SVGTextElement, number>('text')
      .data(steps.filter((_, i) => i % labelEvery === 0))
      .join('text')
      .attr('x', (d) => {
        const idx = steps.indexOf(d)
        return NAME_COL_W + LEFT_PAD + idx * (cellW + CELL_GAP) + cellW / 2
      })
      .attr('y', TOP_AXIS_H - 6)
      .attr('text-anchor', 'middle')
      .attr('font-family', 'JetBrains Mono, monospace')
      .attr('font-size', 9)
      .attr('fill', N.n7)
      .text((d) => String(d))

    // Rows
    paramNames.forEach((pname, rowIdx) => {
      const y = TOP_AXIS_H + rowIdx * ROW_H
      const isSelected = pname === selectedParam
      const rowG = root.append('g')

      // Truncate name from the left so the unique suffix is visible
      const displayName = pname.length > 11 ? '\u2026' + pname.slice(-10) : pname

      rowG
        .append('text')
        .attr('x', NAME_COL_W - 4)
        .attr('y', y + ROW_H * 0.72)
        .attr('text-anchor', 'end')
        .attr('font-family', 'JetBrains Mono, monospace')
        .attr('font-size', 9)
        .attr('fill', isSelected ? N.n9 : N.n7)
        .style('cursor', 'pointer')
        .text(displayName)
        .on('click', () => setSelectedParam((prev) => (prev === pname ? null : pname)))

      // Cells
      steps.forEach((step, colIdx) => {
        const norm = matrix.get(`${pname}::${colIdx}`) ?? 0
        const x = NAME_COL_W + LEFT_PAD + colIdx * (cellW + CELL_GAP)

        rowG
          .append('rect')
          .attr('x', x)
          .attr('y', y)
          .attr('width', cellW)
          .attr('height', ROW_H - 1)
          .attr('rx', 1)
          .attr('fill', cellColor(norm))
          .style('cursor', 'pointer')
          .on('mousemove', function (event: MouseEvent) {
            const tooltip = tooltipRef.current
            if (!tooltip) return
            const rect = (svg.parentElement as HTMLElement).getBoundingClientRect()
            tooltip.style.left = `${event.clientX - rect.left + 12}px`
            tooltip.style.top = `${event.clientY - rect.top - 28}px`
            tooltip.style.display = 'block'
            tooltip.innerHTML =
              `<span style="color:${N.n7}">${displayName}</span><br/>` +
              `step <span style="color:${N.n9}">${ends[colIdx] !== step ? `${step}–${ends[colIdx]}` : step}</span>&nbsp;&nbsp;` +
              `grad_norm <span style="color:${HEAT[4]}">${norm > 0 ? norm.toExponential(3) : '—'}</span>`
          })
          .on('mouseleave', () => {
            if (tooltipRef.current) tooltipRef.current.style.display = 'none'
          })
          .on('click', () => !justDragged.current && setSelectedParam((prev) => (prev === pname ? null : pname)))
      })
    })
  }, [stats, containerWidth, selectedParam, maxCols])

  // Detail data for selected param
  const selectedStats = selectedParam ? stats.filter((s) => s.param_name === selectedParam) : []
  const gradNormSeries = selectedStats.map((s) => ({
    step: s.step,
    timestamp: s.timestamp,
    name: 'grad_norm',
    value: s.grad_norm,
  }))
  const weightNormSeries = selectedStats.map((s) => ({
    step: s.step,
    timestamp: s.timestamp,
    name: 'weight_norm',
    value: s.weight_norm,
  }))

  const fetchRange = runId && selectedParam
    ? async (name: string, q: RangeQuery, maxPoints: number): Promise<Metric[]> => {
        const rows = await api.getParamStats(runId, selectedParam, { ...q, max_points: maxPoints })
        const key = name === 'weight_norm' ? 'weight_norm' : 'grad_norm'
        return rows.map((r) => ({ step: r.step, timestamp: r.timestamp, name, value: r[key] }))
      }
    : undefined

  if (stats.length === 0) return null

  return (
    <div className="rounded-sm border border-py-border bg-py-surface">
      {/* Header + legend */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-py-border px-3 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px] font-semibold text-py-text">Gradient norm heatmap</span>
          {runId && !zoomFetch.zoom && <span className="text-[11px] text-py-faint">Drag across columns to zoom</span>}
          {zoomFetch.zoom && (
            <>
              <span className="num text-[11px] text-py-soft">
                steps {Math.floor(zoomFetch.zoom.x0)}-{Math.ceil(zoomFetch.zoom.x1)}{zoomFetch.loading ? '…' : ''}
              </span>
              <button
                type="button"
                onClick={zoomFetch.reset}
                className="h-6 rounded-sm border border-py-border-hi bg-py-surface px-2 text-xs text-py-soft transition-colors duration-150 ease-out hover:bg-py-elevated hover:text-py-text"
              >
                Reset zoom
              </button>
            </>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="num text-[10px] text-py-muted">low</span>
          <div className="h-2 w-28 rounded-[1px]" style={{ background: HEAT_GRADIENT }} />
          <span className="num text-[10px] text-py-muted">high (log)</span>
        </div>
      </div>

      {/* Heatmap scroll container */}
      <div
        ref={containerRef}
        style={{ overflowX: 'auto', overflowY: 'auto', maxHeight: 480, position: 'relative' }}
      >
        <svg ref={svgRef} style={{ display: 'block', userSelect: 'none' }} {...dragHandlers} />
        {selBox && (
          <div
            style={{
              position: 'absolute', top: 0, bottom: 0, left: selBox.left, width: selBox.width,
              backgroundColor: `${N.n8}14`, border: `1px solid ${N.n7}`, pointerEvents: 'none',
            }}
          />
        )}
        <div
          ref={tooltipRef}
          style={{
            display: 'none',
            position: 'absolute',
            pointerEvents: 'none',
            backgroundColor: N.n2,
            border: `1px solid ${N.n5}`,
            borderRadius: 2,
            padding: '4px 8px',
            fontSize: 11,
            fontFamily: 'JetBrains Mono, monospace',
            color: N.n9,
            whiteSpace: 'nowrap',
            zIndex: 10,
          }}
        />
      </div>

      {/* Detail panel for selected param */}
      {selectedParam && selectedStats.length > 0 && (
        <div className="border-t border-py-border">
          <div className="flex items-center justify-between border-b border-py-border px-3 py-2">
            <span className="num max-w-[420px] truncate text-xs text-py-text" title={selectedParam}>
              {selectedParam}
            </span>
            <button
              type="button"
              onClick={() => setSelectedParam(null)}
              aria-label="Close parameter detail"
              title="Close"
              className="ml-2 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-sm text-py-muted transition-colors duration-150 ease-out hover:bg-py-elevated hover:text-py-text"
            >
              <Icon name="x" size={14} />
            </button>
          </div>
          <div
            className="grid gap-3 p-3"
            style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 280px), 1fr))' }}
          >
            {gradNormSeries.length > 0 && (
              <MetricChart key={`g${selectedParam}`} name="grad_norm" data={gradNormSeries} lineColor={HEAT[4]} source={DETAIL_SOURCE} fetchRange={fetchRange} refreshKey={refreshKey} />
            )}
            {weightNormSeries.length > 0 && (
              <MetricChart key={`w${selectedParam}`} name="weight_norm" data={weightNormSeries} lineColor={STEEL} source={DETAIL_SOURCE} fetchRange={fetchRange} refreshKey={refreshKey} />
            )}
          </div>
        </div>
      )}
    </div>
  )
}

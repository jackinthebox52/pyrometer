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

// Dark → amber → red gradient on log scale
function normColor(t: number): string {
  if (t < 0.5) {
    const u = t * 2
    return `rgb(${Math.round(30 + u * 215)},${Math.round(20 + u * 138)},${Math.round(40 - u * 29)})`
  }
  const u = (t - 0.5) * 2
  return `rgb(${Math.round(245 - u * 6)},${Math.round(158 - u * 90)},${Math.round(11 + u * 57)})`
}

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
      if (!norm || norm <= 0) return '#1a1d24'
      const logVal = (Math.log10(Math.max(norm, 1e-10)) - logMin) / logRange
      return normColor(Math.max(0, Math.min(1, logVal)))
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
      .attr('fill', '#4a4e60')
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
        .attr('fill', isSelected ? '#f59e0b' : '#4a4e60')
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
              `<span style="color:#8b8fa8">${displayName}</span><br/>` +
              `step <span style="color:#f59e0b">${ends[colIdx] !== step ? `${step}–${ends[colIdx]}` : step}</span>&nbsp;&nbsp;` +
              `grad_norm <span style="color:#f59e0b">${norm > 0 ? norm.toExponential(3) : '—'}</span>`
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
    <div className="rounded-lg" style={{ backgroundColor: '#111318', border: '1px solid #252830' }}>
      {/* Header + legend */}
      <div
        className="flex items-center justify-between px-4 pt-3 pb-2"
        style={{ borderBottom: '1px solid #252830' }}
      >
        <span className="text-xs font-mono" style={{ color: '#8b8fa8' }}>
          Gradient Norm Heatmap
          {runId && !zoomFetch.zoom && <span style={{ color: '#4a4e60' }}> · drag across columns to zoom</span>}
          {zoomFetch.zoom && (
            <>
              <span style={{ color: '#f59e0b' }}>
                {' '}· steps {Math.floor(zoomFetch.zoom.x0)}–{Math.ceil(zoomFetch.zoom.x1)}{zoomFetch.loading ? ' …' : ''}
              </span>
              <button
                onClick={zoomFetch.reset}
                className="ml-2 px-1.5 rounded"
                style={{ background: 'none', border: '1px solid #252830', color: '#8b8fa8', cursor: 'pointer' }}
              >
                reset zoom
              </button>
            </>
          )}
        </span>
        <div className="flex items-center gap-2">
          <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>
            low
          </span>
          <div
            style={{
              width: 72,
              height: 8,
              borderRadius: 2,
              background: 'linear-gradient(to right, #1e1428, #f59e0b, #ef4444)',
            }}
          />
          <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>
            high (log)
          </span>
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
              background: 'rgba(245,158,11,0.15)', border: '1px solid rgba(245,158,11,0.6)', pointerEvents: 'none',
            }}
          />
        )}
        <div
          ref={tooltipRef}
          style={{
            display: 'none',
            position: 'absolute',
            pointerEvents: 'none',
            backgroundColor: '#1a1d24',
            border: '1px solid #252830',
            borderRadius: 4,
            padding: '4px 8px',
            fontSize: 11,
            fontFamily: 'JetBrains Mono, monospace',
            color: '#e8eaf0',
            whiteSpace: 'nowrap',
            zIndex: 10,
          }}
        />
      </div>

      {/* Detail panel for selected param */}
      {selectedParam && selectedStats.length > 0 && (
        <div style={{ borderTop: '1px solid #252830' }}>
          <div
            className="flex items-center justify-between px-4 py-2"
            style={{ borderBottom: '1px solid #1a1d24' }}
          >
            <span
              className="text-xs font-mono truncate"
              style={{ color: '#f59e0b', maxWidth: 420 }}
              title={selectedParam}
            >
              {selectedParam}
            </span>
            <button
              onClick={() => setSelectedParam(null)}
              style={{
                background: 'none',
                border: 'none',
                color: '#4a4e60',
                cursor: 'pointer',
                fontSize: 14,
                lineHeight: 1,
                flexShrink: 0,
                marginLeft: 8,
              }}
            >
              ✕
            </button>
          </div>
          <div
            className="grid gap-4 p-4"
            style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))' }}
          >
            {gradNormSeries.length > 0 && (
              <MetricChart key={`g${selectedParam}`} name="grad_norm" data={gradNormSeries} lineColor="#f59e0b" source={DETAIL_SOURCE} fetchRange={fetchRange} refreshKey={refreshKey} />
            )}
            {weightNormSeries.length > 0 && (
              <MetricChart key={`w${selectedParam}`} name="weight_norm" data={weightNormSeries} lineColor="#60a5fa" source={DETAIL_SOURCE} fetchRange={fetchRange} refreshKey={refreshKey} />
            )}
          </div>
        </div>
      )}
    </div>
  )
}

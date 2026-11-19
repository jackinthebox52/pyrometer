import * as d3 from 'd3'
import { useEffect, useRef, useState } from 'react'
import type { ParamStat } from '../api/types.ts'
import MetricChart from './MetricChart.tsx'

interface Props {
  stats: ParamStat[]
}

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

export default function GradientHeatmap({ stats }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const [containerWidth, setContainerWidth] = useState(0)
  const [selectedParam, setSelectedParam] = useState<string | null>(null)

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

    const steps = [...new Set(stats.map((s) => s.step))].sort((a, b) => a - b)

    if (paramNames.length === 0 || steps.length === 0) return

    // (paramName, step) → grad_norm lookup
    const matrix = new Map<string, number>()
    for (const s of stats) {
      matrix.set(`${s.param_name}::${s.step}`, s.grad_norm)
    }

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
        const norm = matrix.get(`${pname}::${step}`) ?? 0
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
              `step <span style="color:#f59e0b">${step}</span>&nbsp;&nbsp;` +
              `grad_norm <span style="color:#f59e0b">${norm > 0 ? norm.toExponential(3) : '—'}</span>`
          })
          .on('mouseleave', () => {
            if (tooltipRef.current) tooltipRef.current.style.display = 'none'
          })
          .on('click', () => setSelectedParam((prev) => (prev === pname ? null : pname)))
      })
    })
  }, [stats, containerWidth, selectedParam])

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
        <svg ref={svgRef} style={{ display: 'block' }} />
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
              <MetricChart name="grad_norm" data={gradNormSeries} lineColor="#f59e0b" />
            )}
            {weightNormSeries.length > 0 && (
              <MetricChart name="weight_norm" data={weightNormSeries} lineColor="#60a5fa" />
            )}
          </div>
        </div>
      )}
    </div>
  )
}

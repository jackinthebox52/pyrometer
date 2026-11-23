import { useEffect, useRef, useState } from 'react'
import * as d3 from 'd3'
import type { ProfileEvent } from '../api/types.ts'
import { formatMicroseconds } from '../utils/format.ts'
import { LEGEND, opColor } from '../utils/kernelColors.ts'
import { HEAT, N, STEEL } from '../theme.ts'
import ExpandModal, { ExpandButton } from './ExpandModal.tsx'

interface Props {
  events: ProfileEvent[]
}

const PAD = { top: 8, right: 16, bottom: 40, left: 72 }
const LANE_H = 42
const LANE_GAP = 14
const TOOLTIP_W = 260

interface BarDatum {
  x0: number        // start in μs (within this lane)
  x1: number        // end in μs
  name: string
  cpuUs: number
  cudaUs: number
  color: string
  inputShapes: number[][]
  memoryBytes: number | null
  isOther: boolean  // true = aggregated "other" bar, no tooltip
}

export default function KernelTimeline({ events }: Props) {
  const svgRef = useRef<SVGSVGElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const [isExpanded, setIsExpanded] = useState(false)
  const [containerWidth, setContainerWidth] = useState(0)

  // Track container width so d3 redraws on resize (e.g. modal open, window resize)
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

  useEffect(() => {
    if (!svgRef.current || !containerRef.current || events.length === 0) return

    const width = containerWidth || containerRef.current.clientWidth
    const svg = d3.select(svgRef.current)
    svg.selectAll('*').remove()

    const cpuTotal = events.reduce((s, e) => s + e.cpu_time_us, 0)
    const cudaTotal = events.reduce((s, e) => s + e.cuda_time_us, 0)
    const maxUs = Math.max(cpuTotal, cudaTotal)
    if (maxUs === 0) return

    const innerW = width - PAD.left - PAD.right
    const xBase = d3.scaleLinear().domain([0, maxUs]).range([0, innerW])

    // Build bar data for a lane. Returns BarDatum[] with x0/x1 in μs coordinates.
    function buildBars(key: 'cpu_time_us' | 'cuda_time_us', laneTotal: number): BarDatum[] {
      if (laneTotal === 0) return []
      let cursor = 0
      const displayed = events
        .filter((e) => e[key] / laneTotal >= 0.002)
        .sort((a, b) => b[key] - a[key])
        .slice(0, 50)

      const bars: BarDatum[] = displayed.map((e) => {
        const us = e[key]
        const x0 = cursor
        cursor += us
        return {
          x0,
          x1: cursor,
          name: e.name,
          cpuUs: e.cpu_time_us,
          cudaUs: e.cuda_time_us,
          color: opColor(e.name, e.category),
          inputShapes: e.input_shapes ?? [],
          memoryBytes: e.memory_bytes,
          isOther: false,
        }
      })

      // Remainder → single "other" bar
      const remainUs = laneTotal - cursor
      if (remainUs > 0) {
        bars.push({
          x0: cursor,
          x1: cursor + remainUs,
          name: '(other)',
          cpuUs: 0,
          cudaUs: 0,
          color: N.n4,
          inputShapes: [],
          memoryBytes: null,
          isOther: true,
        })
      }
      return bars
    }

    const lanesRaw = [
      { label: 'CPU', bars: buildBars('cpu_time_us', cpuTotal), totalUs: cpuTotal },
      { label: 'CUDA', bars: buildBars('cuda_time_us', cudaTotal), totalUs: cudaTotal },
    ].filter((l) => l.bars.filter((b) => !b.isOther).length > 0)

    if (lanesRaw.length === 0) return

    const innerH = lanesRaw.length * LANE_H + (lanesRaw.length - 1) * LANE_GAP
    const svgH = PAD.top + innerH + PAD.bottom
    svg.attr('width', width).attr('height', svgH)

    // Clip path — prevents bars from overflowing during pan
    svg
      .append('defs')
      .append('clipPath')
      .attr('id', 'tl-clip')
      .append('rect')
      .attr('x', 0)
      .attr('y', 0)
      .attr('width', innerW)
      .attr('height', innerH)

    const g = svg.append('g').attr('transform', `translate(${PAD.left},${PAD.top})`)

    // X axis
    const xAxisFn = d3
      .axisBottom<number>(xBase)
      .ticks(5)
      .tickFormat((d) => formatMicroseconds(d))
      .tickSize(-innerH)

    const axisG = g.append('g').attr('transform', `translate(0,${innerH})`).call(xAxisFn)

    function styleAxis(ag: d3.Selection<SVGGElement, unknown, null, undefined>) {
      ag.select('.domain').remove()
      ag.selectAll('.tick line').attr('stroke', N.n4).attr('stroke-dasharray', '2,4')
      ag
        .selectAll('.tick text')
        .attr('fill', N.n7)
        .attr('font-size', 10)
        .attr('font-family', 'JetBrains Mono, monospace')
        .attr('dy', 14)
    }
    styleAxis(axisG)

    const tooltip = d3.select(tooltipRef.current!)

    // Per-lane groups that need updating on zoom
    const laneRefs: Array<{
      barsG: d3.Selection<SVGGElement, unknown, null, undefined>
      labelsG: d3.Selection<SVGGElement, unknown, null, undefined>
    }> = []

    lanesRaw.forEach((lane, li) => {
      const y0 = li * (LANE_H + LANE_GAP)
      const laneG = g.append('g').attr('transform', `translate(0,${y0})`)

      // Lane label (outside clip, not updated on zoom)
      laneG
        .append('text')
        .attr('x', -8)
        .attr('y', LANE_H / 2)
        .attr('text-anchor', 'end')
        .attr('dominant-baseline', 'middle')
        .attr('fill', N.n7)
        .attr('font-size', 10)
        .attr('font-family', 'JetBrains Mono, monospace')
        .text(lane.label)

      // Lane background (full width, outside clip)
      laneG
        .append('rect')
        .attr('x', 0)
        .attr('y', 0)
        .attr('width', innerW)
        .attr('height', LANE_H)
        .attr('fill', N.n1)
        .attr('rx', 2)

      // Clipped content group
      const clippedG = laneG.append('g').attr('clip-path', 'url(#tl-clip)')

      // Bars (data-bound for zoom updates)
      const barsG = clippedG.append('g')
      barsG
        .selectAll<SVGRectElement, BarDatum>('rect')
        .data(lane.bars)
        .join('rect')
        .attr('x', (d) => xBase(d.x0))
        .attr('y', 2)
        .attr('width', (d) => Math.max(xBase(d.x1) - xBase(d.x0) - 1, 0.5))
        .attr('height', LANE_H - 4)
        .attr('fill', (d) => d.color)
        .attr('rx', 2)
        .attr('opacity', (d) => (d.isOther ? 0.7 : 0.82))
        .style('cursor', 'default')
        .on('mouseover', function (this: SVGRectElement, _ev: Event, d: BarDatum) {
          if (d.isOther) return
          d3.select(this).attr('opacity', 1)
          const shapes =
            d.inputShapes.length > 0
              ? d.inputShapes.map((s) => `[${s.join('×')}]`).join(', ')
              : '—'
          const memLine =
            d.memoryBytes != null
              ? `<div style="color:${N.n7}">Mem delta: <span style="color:${STEEL}">${(d.memoryBytes / 1024).toFixed(1)} KB</span></div>`
              : ''
          tooltip.style('display', 'block').html(
            `<div style="color:${N.n9};margin-bottom:4px;word-break:break-all">${d.name}</div>` +
              `<div style="color:${N.n7}">CPU: <span style="color:${STEEL}">${formatMicroseconds(d.cpuUs)}</span></div>` +
              `<div style="color:${N.n7}">CUDA: <span style="color:${HEAT[4]}">${formatMicroseconds(d.cudaUs)}</span></div>` +
              memLine +
              `<div style="color:${N.n6};margin-top:3px;font-size:10px">shapes: ${shapes}</div>`,
          )
        })
        .on('mousemove', function (_ev: Event, d: BarDatum) {
          if (d.isOther) return
          const ev = _ev as MouseEvent
          const bounds = containerRef.current!.getBoundingClientRect()
          const cursorX = ev.clientX - bounds.left
          // Flip tooltip to the left when near the right edge
          const left =
            cursorX + TOOLTIP_W + 20 > bounds.width
              ? `${cursorX - TOOLTIP_W - 10}px`
              : `${cursorX + 14}px`
          tooltip
            .style('left', left)
            .style('top', `${ev.clientY - bounds.top - 14}px`)
        })
        .on('mouseout', function (this: SVGRectElement, _ev: Event, d: BarDatum) {
          if (d.isOther) return
          d3.select(this).attr('opacity', 0.82)
          tooltip.style('display', 'none')
        })

      // Inline labels (data-bound, show/hide based on bar width)
      const labelsG = clippedG.append('g').attr('pointer-events', 'none')
      labelsG
        .selectAll<SVGTextElement, BarDatum>('text')
        .data(lane.bars.filter((b) => !b.isOther))
        .join('text')
        .attr('x', (d) => xBase(d.x0) + (xBase(d.x1) - xBase(d.x0)) / 2)
        .attr('y', LANE_H / 2 + 1)
        .attr('text-anchor', 'middle')
        .attr('dominant-baseline', 'middle')
        .attr('fill', N.n0)
        .attr('font-size', 9)
        .attr('font-family', 'JetBrains Mono, monospace')
        .style('display', (d) => (xBase(d.x1) - xBase(d.x0) > 48 ? null : 'none'))
        .text((d) => {
          const bw = xBase(d.x1) - xBase(d.x0)
          const maxChars = Math.max(1, Math.floor((bw - 10) / 6.2))
          return d.name.length > maxChars ? d.name.slice(0, maxChars - 1) + '…' : d.name
        })

      laneRefs.push({ barsG, labelsG })
    })

    // ── Zoom ──────────────────────────────────────────────────────────────────
    const zoom = d3
      .zoom<SVGSVGElement, unknown>()
      .scaleExtent([1, 60])
      .extent([
        [0, 0],
        [innerW, innerH],
      ])
      .translateExtent([
        [0, 0],
        [innerW, innerH],
      ])
      .on('zoom', (event: d3.D3ZoomEvent<SVGSVGElement, unknown>) => {
        const xNew = event.transform.rescaleX(xBase)

        // Update axis
        axisG.call(xAxisFn.scale(xNew as d3.AxisScale<number>))
        styleAxis(axisG)

        // Update bars and labels in all lanes
        laneRefs.forEach(({ barsG, labelsG }) => {
          barsG
            .selectAll<SVGRectElement, BarDatum>('rect')
            .attr('x', (d) => xNew(d.x0))
            .attr('width', (d) => Math.max(xNew(d.x1) - xNew(d.x0) - 1, 0.5))

          labelsG
            .selectAll<SVGTextElement, BarDatum>('text')
            .attr('x', (d) => xNew(d.x0) + (xNew(d.x1) - xNew(d.x0)) / 2)
            .style('display', (d) => (xNew(d.x1) - xNew(d.x0) > 48 ? null : 'none'))
            .text((d) => {
              const bw = xNew(d.x1) - xNew(d.x0)
              const maxChars = Math.max(1, Math.floor((bw - 10) / 6.2))
              return d.name.length > maxChars ? d.name.slice(0, maxChars - 1) + '…' : d.name
            })
        })
      })

    svg.call(zoom).style('cursor', 'grab')
    // Reset zoom on double-click
    svg.on('dblclick.zoom', () => {
      svg.transition().duration(300).call(zoom.transform, d3.zoomIdentity)
    })

    // Prevent page scroll while hovering the timeline, even at zoom limits
    const svgEl = svgRef.current!
    const stopScroll = (e: WheelEvent) => e.preventDefault()
    svgEl.addEventListener('wheel', stopScroll, { passive: false })
    return () => svgEl.removeEventListener('wheel', stopScroll)
  }, [events, containerWidth])

  if (events.length === 0) return null

  return (
    <div
      ref={containerRef}
      className="relative rounded-sm border border-py-border bg-py-surface p-3"
    >
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[13px] font-semibold text-py-text">Kernel timeline</span>
        <div className="flex items-center gap-3">
          <span className="text-[11px] text-py-faint">
            Scroll to zoom, drag to pan, double-click to reset
          </span>
          <ExpandButton onClick={() => setIsExpanded(true)} />
        </div>
      </div>

      <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1.5">
        {LEGEND.map(({ color, label }) => (
          <div key={label} className="flex items-center gap-1.5">
            <div className="h-2 w-2 rounded-[1px]" style={{ backgroundColor: color }} />
            <span className="text-[11px] text-py-muted">{label}</span>
          </div>
        ))}
      </div>

      <svg ref={svgRef} style={{ width: '100%', display: 'block' }} />

      <div
        ref={tooltipRef}
        style={{
          display: 'none',
          position: 'absolute',
          background: N.n2,
          border: `1px solid ${N.n5}`,
          borderRadius: 2,
          padding: '7px 10px',
          fontFamily: 'JetBrains Mono, monospace',
          fontSize: 11,
          color: N.n9,
          zIndex: 50,
          width: TOOLTIP_W,
          lineHeight: 1.65,
          pointerEvents: 'none',
        }}
      />

      {isExpanded && (
        <ExpandModal onClose={() => setIsExpanded(false)}>
          {/* key forces full d3 remount so it reads the modal container width */}
          <KernelTimeline key="expanded" events={events} />
        </ExpandModal>
      )}
    </div>
  )
}

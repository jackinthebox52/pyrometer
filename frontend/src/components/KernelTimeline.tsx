import { useEffect, useRef, useState } from 'react'
import * as d3 from 'd3'
import type { ProfileEvent } from '../api/types.ts'
import { formatMicroseconds } from '../utils/format.ts'
import ExpandModal, { ExpandButton } from './ExpandModal.tsx'

interface Props {
  events: ProfileEvent[]
}

const PAD = { top: 8, right: 16, bottom: 40, left: 72 }
const LANE_H = 42
const LANE_GAP = 14
const TOOLTIP_W = 260

const LEGEND = [
  { color: '#3b82f6', label: 'matmul' },
  { color: '#4ade80', label: 'conv' },
  { color: '#f59e0b', label: 'attention' },
  { color: '#a78bfa', label: 'norm' },
  { color: '#818cf8', label: 'activation' },
  { color: '#ec4899', label: 'loss' },
  { color: '#f43f5e', label: 'optimizer' },
  { color: '#c084fc', label: 'autograd' },
  { color: '#84cc16', label: 'reduction' },
  { color: '#06b6d4', label: 'shape/layout' },
  { color: '#6b7280', label: 'elementwise' },
  { color: '#f97316', label: 'transfer' },
  { color: '#22c55e', label: 'memory/fill' },
  { color: '#94a3b8', label: 'cuda overhead' },
  { color: '#4a4e60', label: 'other' },
] as const

function opColor(name: string, category: string): string {
  if (category === 'memory_alloc' || category === 'memory_free') return '#22c55e'
  const n = name.toLowerCase()

  // Optimizer — Optimizer.step#*, Optimizer.zero_grad#*, _foreach_* (Adam/SGD multi-tensor ops)
  if (/optimizer\.(step|zero_grad)/.test(n)) return '#f43f5e'
  if (/_foreach_/.test(n)) return '#f43f5e'

  // CUDA API overhead & profiler artifacts
  if (/^cuda(launchkernel|memcpyasync|memsetasync|streamsynchronize|devicesynchronize|occupancy|streamiscapturing)/.test(n)) return '#94a3b8'
  if (/activity buffer|lazy function loading/.test(n)) return '#94a3b8'

  // Autograd engine — evaluate_function wrappers, named backward nodes, detach ops
  if (/autograd::engine|accumulategrad/.test(n)) return '#c084fc'
  if (/backward0/.test(n)) return '#c084fc'
  if (/^detach|::detach/.test(n)) return '#c084fc'

  // GEMM / matmul — addmm is nn.Linear; bmm/mm are direct; sgemm/hgemm are raw CUDA kernels
  if (/(addmm|baddbmm|matmul|sgemm|hgemm|dgemm|cublas|cutlass)/.test(n)) return '#3b82f6'
  if (/(?:^|::)(mm|bmm)(?:$|_)/.test(n)) return '#3b82f6'
  if (/(?:^|::)linear(?:$|_)/.test(n)) return '#3b82f6'

  // Convolution
  if (/conv/.test(n)) return '#4ade80'

  // Data transfer
  if (/(copy_|(?:^|::)to(?:$|\s)|^memcpy|cudamemcpy)/.test(n)) return '#f97316'

  // Normalization — layer_norm, batch_norm, linalg_vector_norm (gradient clipping), etc.
  if (/(layer_norm|batch_norm|group_norm|instance_norm|rms_norm|linalg_vector_norm|frobenius_norm)/.test(n)) return '#a78bfa'

  // Attention / softmax / log-softmax
  if (/(attention|attn|softmax|scaled_dot|flash_attn|log_softmax|softmax_warp)/.test(n)) return '#f59e0b'

  // Embedding
  if (/embedding/.test(n)) return '#14b8a6'

  // Activations + dropout (both are element-wise non-linear ops)
  if (/(relu|gelu|silu|sigmoid|tanh|hardswish|leaky_relu|threshold_backward|fused_dropout|native_dropout|dropout)/.test(n)) return '#818cf8'

  // Loss functions
  if (/(cross_entropy|nll_loss|mse_loss|bce|kl_div|hinge|margin_loss)/.test(n)) return '#ec4899'

  // Raw CUDA kernel name heuristics (long mangled names from cuda_kernel category)
  if (/reduce_kernel/.test(n)) return '#84cc16'           // reduction kernels
  if (/multi_tensor_apply_kernel/.test(n)) return '#f43f5e' // optimizer multi-tensor ops
  if (/vectorized_elementwise_kernel/.test(n)) return '#6b7280'
  if (/nll_loss.*kernel/.test(n)) return '#ec4899'
  if (/dropout_kernel|masked_scale/.test(n)) return '#818cf8'

  // Shape / layout ops — including aten::t (the .T transpose shorthand)
  if (/(view|reshape|transpose|permute|contiguous|flatten|squeeze|unsqueeze|expand|repeat|chunk|split|narrow|select|as_strided|(?:^|::)cat(?:$|_)|(?:^|::)stack(?:$|_))/.test(n)) return '#06b6d4'
  if (/(?:^|::)t(?:$|_)/.test(n)) return '#06b6d4'

  // Allocations, fill, memset — empty, zeros, fill_, zero_, Memset (Device)
  if (/^memset|(?:^|::)(empty|zeros|ones|full|fill_|zero_|empty_like|zeros_like|ones_like|empty_strided)(?:_|$|::)/.test(n)) return '#22c55e'

  // Reductions
  if (/(?:^|::)(sum|mean|amax|amin|max|min|var|std|norm|prod|cumsum|cumprod|logsumexp|argmax|argmin)(?:_|$|::)/.test(n)) return '#84cc16'

  // Elementwise arithmetic
  if (/(?:^|::)(add|mul|sub|div|pow|exp|log|sqrt|abs|neg|clamp|where|sign|ceil|floor|round|lerp|addcmul|addcdiv|clamp_min|clamp_max)(?:_|$|::)/.test(n)) return '#6b7280'

  // Indexing
  if (/(index|gather|scatter|masked_fill|masked_select|nonzero)/.test(n)) return '#fb923c'

  return '#4a4e60'
}

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
          color: '#252830',
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
      ag.selectAll('.tick line').attr('stroke', '#1e2128').attr('stroke-dasharray', '3,2')
      ag
        .selectAll('.tick text')
        .attr('fill', '#4a4e60')
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
        .attr('fill', '#4a4e60')
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
        .attr('fill', '#0e1014')
        .attr('rx', 3)

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
              ? `<div style="color:#8b8fa8">Δmem: <span style="color:#60a5fa">${(d.memoryBytes / 1024).toFixed(1)} KB</span></div>`
              : ''
          tooltip.style('display', 'block').html(
            `<div style="color:#e8eaf0;margin-bottom:4px;word-break:break-all">${d.name}</div>` +
              `<div style="color:#8b8fa8">CPU: <span style="color:#e8eaf0">${formatMicroseconds(d.cpuUs)}</span></div>` +
              `<div style="color:#8b8fa8">CUDA: <span style="color:#f59e0b">${formatMicroseconds(d.cudaUs)}</span></div>` +
              memLine +
              `<div style="color:#4a4e60;margin-top:3px;font-size:10px">shapes: ${shapes}</div>`,
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
        .attr('fill', 'rgba(0,0,0,0.72)')
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
      className="rounded-lg p-4"
      style={{ backgroundColor: '#111318', border: '1px solid #252830', position: 'relative' }}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-mono" style={{ color: '#8b8fa8' }}>
          Kernel Timeline
        </span>
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>
            scroll to zoom · drag to pan · double-click to reset
          </span>
          <ExpandButton onClick={() => setIsExpanded(true)} />
        </div>
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1.5 mb-3">
        {LEGEND.map(({ color, label }) => (
          <div key={label} className="flex items-center gap-1.5">
            <div className="w-2 h-2 rounded-sm" style={{ backgroundColor: color }} />
            <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>
              {label}
            </span>
          </div>
        ))}
      </div>

      <svg ref={svgRef} style={{ width: '100%', display: 'block' }} />

      <div
        ref={tooltipRef}
        style={{
          display: 'none',
          position: 'absolute',
          background: '#1a1d24',
          border: '1px solid #353840',
          borderRadius: 6,
          padding: '7px 10px',
          fontFamily: 'JetBrains Mono, monospace',
          fontSize: 11,
          color: '#e8eaf0',
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

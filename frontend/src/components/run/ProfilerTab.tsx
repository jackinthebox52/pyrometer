import type { ProfileEvent } from '../../api/types.ts'
import { formatMicroseconds } from '../../utils/format.ts'
import KernelTimeline from '../KernelTimeline.tsx'
import MemoryDeltaChart from '../MemoryDeltaChart.tsx'
import OperatorBreakdown from '../OperatorBreakdown.tsx'
import { Heading } from './shared.tsx'

function OptimizationHints({ events }: { events: ProfileEvent[] }) {
  if (events.length === 0) return null
  const totalUs = events.reduce((acc, e) => acc + e.duration_us, 0)
  const totalCudaUs = events.reduce((acc, e) => acc + e.cuda_time_us, 0)
  const totalCpuUs = events.reduce((acc, e) => acc + e.cpu_time_us, 0)
  const hints: string[] = []

  // Excessive host-to-device data transfer
  const transferUs = events.filter((e) => e.name.includes('aten::to') || e.name.includes('copy_')).reduce((acc, e) => acc + e.duration_us, 0)
  if (totalUs > 0 && transferUs / totalUs > 0.05) {
    hints.push(
      `${((transferUs / totalUs) * 100).toFixed(0)}% of step time in data transfers (aten::to / copy_). ` +
        `Consider pin_memory=True in DataLoader to speed up host-to-device transfers.`,
    )
  }
  // CPU-bound step
  if (totalCudaUs > 0 && totalCpuUs / totalCudaUs > 2.0) {
    hints.push(
      `CPU time (${formatMicroseconds(totalCpuUs)}) is more than 2x GPU time (${formatMicroseconds(totalCudaUs)}). ` +
        `The GPU may be underutilised. Consider torch.compile(), a larger batch size, or more DataLoader workers.`,
    )
  }
  // Many small allocations
  const smallAllocCount = events.filter((e) => e.memory_bytes != null && e.memory_bytes > 0 && e.memory_bytes < 1024 * 1024).length
  if (smallAllocCount > 10) {
    hints.push(
      `${smallAllocCount} operators making allocations under 1 MB. Pre-allocating output buffers or wrapping ` +
        `inference with torch.no_grad() can reduce activation memory overhead.`,
    )
  }
  if (hints.length === 0) return null

  return (
    <section className="rounded border border-py-border bg-py-surface p-4" aria-label="Optimization hints">
      <h3 className="mb-2 text-[13px] font-semibold text-py-text">Optimization hints</h3>
      <ul className="flex flex-col gap-2">
        {hints.map((hint, i) => (
          <li key={i} className="flex gap-2 text-[13px] leading-relaxed text-py-soft">
            <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-[1px] bg-py-heat-4" aria-hidden="true" />
            {hint}
          </li>
        ))}
      </ul>
    </section>
  )
}

interface Props {
  steps: number[]
  selected: number | null
  onSelect: (step: number) => void
  events: ProfileEvent[]
  loading: boolean
}

export default function ProfilerTab({ steps, selected, onSelect, events, loading }: Props) {
  return (
    <div>
      <Heading>Profiler</Heading>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <span className="text-[13px] text-py-muted" id="profiled-step-label">Profiled step</span>
        {steps.length > 8 ? (
          <select
            aria-labelledby="profiled-step-label"
            value={selected ?? ''}
            onChange={(e) => onSelect(Number(e.target.value))}
            className="num h-8 rounded border border-py-border-hi bg-py-elevated px-2 text-[13px] text-py-text"
          >
            {steps.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        ) : (
          <div role="group" aria-labelledby="profiled-step-label" className="inline-flex overflow-hidden rounded border border-py-border-hi">
            {steps.map((s) => (
              <button
                key={s}
                type="button"
                aria-pressed={selected === s}
                onClick={() => onSelect(s)}
                className={`num h-7 border-l border-py-border-hi px-3 text-xs transition-colors first:border-l-0 ${
                  selected === s ? 'bg-py-elevated text-py-text' : 'bg-py-surface text-py-muted hover:bg-py-elevated hover:text-py-soft'
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        )}
      </div>

      {loading ? (
        <div className="flex h-32 items-center justify-center rounded border border-py-border bg-py-surface text-[13px] text-py-muted">Loading profile…</div>
      ) : (
        <div className="flex flex-col gap-4">
          <KernelTimeline events={events} />
          <OperatorBreakdown events={events} />
          <MemoryDeltaChart events={events} />
          <OptimizationHints events={events} />
        </div>
      )}
    </div>
  )
}

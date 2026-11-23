import type { GpuSummary, Metric, Run, RunSummary, SystemSnapshot } from '../../api/types.ts'
import type { XSource } from '../../utils/xAxis.ts'
import { formatBytes } from '../../utils/format.ts'
import { SERIES, STEEL } from '../../theme.ts'
import { RUN_COLORS } from '../../utils/runColors.ts'
import CompareChart from '../CompareChart.tsx'
import type { RunSeries } from '../CompareChart.tsx'
import MetricChart from '../MetricChart.tsx'
import type { ExternalZoom } from '../MetricChart.tsx'
import { CHART_GRID, Heading, Note } from './shared.tsx'

/** GPU snapshots have no steps: x is elapsed time since the first snapshot, or wall-clock time. */
const GPU_AXES = ['rel', 'time'] as const

/** One GPU: "process" is the series colour, "device" steel. Several GPUs: one run colour each, device line at half opacity. */
const DEVICE_OPACITY = 0.5

const SYSTEM_SERIES: Array<{
  key: keyof SystemSnapshot
  preferKey?: keyof SystemSnapshot
  label: string
  color: string
  transform?: (v: number) => number
}> = [
  { key: 'gpu_utilization', preferKey: 'attributed_utilization', label: 'GPU util (%)', color: SERIES },
  { key: 'temperature', label: 'Temperature (°C)', color: SERIES },
  { key: 'power_draw', preferKey: 'attributed_power', label: 'Power (W)', color: SERIES },
  { key: 'memory_used', preferKey: 'process_memory_used', label: 'VRAM (GB)', color: SERIES, transform: (v) => +(v / 1024 ** 3).toFixed(2) },
]

function Meta({ label, value, title }: { label: string; value: string | null | undefined; title?: string }) {
  if (!value) return null
  return (
    <div className="min-w-0">
      <dt className="log-label mb-0.5">{label}</dt>
      <dd className="num whitespace-pre-line break-words text-xs leading-relaxed text-py-text" title={title}>{value}</dd>
    </div>
  )
}

interface Props {
  run: Run
  summary: RunSummary | null
  snapshots: SystemSnapshot[] // sorted by timestamp (zoomed window when zoomed)
  gpuIndices: number[]
  gpuT0: number
  gpuSource: XSource
  gpuZoom: ExternalZoom
  hasSamples: boolean
}

export default function SystemTab({ run, summary, snapshots, gpuIndices, gpuT0, gpuSource, gpuZoom, hasSamples }: Props) {
  const gitLabel = run.git_hash ? `${run.git_hash.slice(0, 8)}${run.git_dirty ? ' (dirty)' : ''}` : null
  const gpuLabel =
    run.gpus && run.gpus.length > 0
      ? run.gpus
          .map((g) => {
            const mem = g.memory_total ? ` · ${formatBytes(g.memory_total)}` : ''
            const mig = g.is_mig ? ' · MIG instance' : g.mig_mode === 'enabled' ? ` · MIG (${g.mig_devices.length})` : ''
            const prefix = run.gpus!.length > 1 ? `${g.index}: ` : ''
            return `${prefix}${g.name ?? 'GPU'}${mem}${mig}`
          })
          .join('\n')
      : [run.gpu_model, run.gpu_memory_total ? formatBytes(run.gpu_memory_total) : null].filter(Boolean).join(' · ')
  const migNote = run.gpus?.some((g) => g.is_mig || g.mig_mode === 'enabled')
    ? 'MIG: NVML reports memory per instance but no utilization, temperature or power'
    : undefined
  const perGpu: GpuSummary[] = (summary?.per_gpu?.length ?? 0) > 1 ? summary!.per_gpu! : []
  const t0 = Number.isFinite(gpuT0) ? gpuT0 : 0

  return (
    <div className="flex flex-col gap-6">
      <section>
        <Heading>GPU stats</Heading>
        {!hasSamples ? (
          <Note>
            {run.status === 'running'
              ? 'No GPU samples yet. They appear here every 2 s once the run reports an NVIDIA GPU.'
              : 'No GPU samples were recorded for this run (no NVIDIA GPU or NVML on the training host).'}
          </Note>
        ) : (
          <div className={CHART_GRID}>
            {SYSTEM_SERIES.map(({ key, preferKey, label, color, transform }) => {
              const conv = (v: number) => (transform ? transform(v) : v)
              const toMetrics = (gpu: number, k: keyof SystemSnapshot, name: string): Metric[] =>
                snapshots
                  .filter((s) => s.gpu_index === gpu && s[k] != null)
                  .map((s) => ({ step: Math.round(s.timestamp - t0), timestamp: s.timestamp, name, value: conv(s[k] as number) }))

              // One line per GPU; "process" (this run's own usage) and "device" (whole GPU, incl. other
              // processes) are drawn separately when per-process numbers exist.
              const hasProcess = preferKey != null && snapshots.some((s) => s[preferKey] != null)
              const multi = gpuIndices.length > 1
              const series: RunSeries[] = []
              gpuIndices.forEach((gpu, i) => {
                const base = multi ? RUN_COLORS[i % RUN_COLORS.length] : color
                const tag = multi ? `GPU ${gpu}` : ''
                if (hasProcess && preferKey) {
                  const n = multi ? `${tag} process` : 'process'
                  series.push({ runId: `${gpu}-p`, name: n, color: base, metrics: toMetrics(gpu, preferKey, n), source: gpuSource })
                  const d = multi ? `${tag} device` : 'device'
                  series.push({ runId: `${gpu}-d`, name: d, color: multi ? base : STEEL, opacity: multi ? DEVICE_OPACITY : undefined, metrics: toMetrics(gpu, key, d), source: gpuSource })
                } else {
                  const n = multi ? tag : label
                  series.push({ runId: `${gpu}-d`, name: n, color: base, metrics: toMetrics(gpu, key, n), source: gpuSource })
                }
              })
              const nonEmpty = series.filter((s) => s.metrics.length > 0)
              if (nonEmpty.length === 0) return null
              const displayLabel = preferKey && !hasProcess ? `${label} (device)` : label
              if (nonEmpty.length === 1) {
                return <MetricChart key={key} name={displayLabel} data={nonEmpty[0].metrics} lineColor={color} zoom={gpuZoom} source={gpuSource} axes={GPU_AXES} />
              }
              return <CompareChart key={key} metricName={displayLabel} series={nonEmpty} zoom={gpuZoom} axes={GPU_AXES} />
            })}
          </div>
        )}
      </section>

      {perGpu.length > 0 && (
        <section>
          <Heading>Per GPU</Heading>
          <div className="overflow-x-auto rounded border border-py-border bg-py-surface">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="border-b border-py-border text-left">
                  <th className="log-label px-3 py-2 font-semibold">GPU</th>
                  <th className="log-label px-3 py-2 font-semibold">Avg util</th>
                  <th className="log-label px-3 py-2 font-semibold">Peak VRAM</th>
                </tr>
              </thead>
              <tbody>
                {perGpu.map((g) => {
                  const peak = g.peak_process_memory_bytes ?? g.peak_memory_bytes
                  return (
                    <tr key={g.gpu_index} className="border-b border-py-border last:border-b-0">
                      <td className="num px-3 py-1.5 text-py-text">{g.gpu_index}</td>
                      <td className="num px-3 py-1.5 text-py-soft">{g.avg_gpu_utilization != null ? `${g.avg_gpu_utilization.toFixed(0)}%` : 'n/a'}</td>
                      <td className="num px-3 py-1.5 text-py-soft">{peak != null ? formatBytes(peak) : 'n/a'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section>
        <Heading>Hardware and environment</Heading>
        <dl className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-x-6 gap-y-3 rounded border border-py-border bg-py-surface p-4">
          <Meta label={run.gpus && run.gpus.length > 1 ? `GPUs (${run.gpus.length})` : 'GPU'} value={gpuLabel || null} title={migNote} />
          <Meta label="Driver" value={run.driver_version ?? run.gpus?.[0]?.driver_version} />
          <Meta label="CUDA" value={run.cuda_version} />
          <Meta label="PyTorch" value={run.pytorch_version} />
          <Meta label="Python" value={run.python_version} />
          <Meta label="Host" value={run.hostname} />
          <Meta label="Git" value={gitLabel} />
        </dl>
      </section>
    </div>
  )
}

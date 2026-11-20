import { MAX_ALPHA } from '../utils/chartOptions.ts'
import type { ChartOptions, Normalization } from '../utils/chartOptions.ts'

interface Props {
  opts: ChartOptions
  onChange: (patch: Partial<ChartOptions>) => void
  /** Number of points hidden because log scale cannot show values <= 0. */
  dropped?: number
  /** Metric names usable as x-axis. When undefined, the x-axis selector is hidden. */
  axisMetrics?: string[]
}

const pill = (active: boolean): React.CSSProperties => ({
  background: active ? '#252830' : 'none',
  border: '1px solid #252830',
  borderRadius: 4,
  padding: '0 6px',
  cursor: 'pointer',
  fontSize: 10,
  lineHeight: '16px',
  color: active ? '#f59e0b' : '#4a4e60',
  fontFamily: 'JetBrains Mono, monospace',
})

/** Per-chart toggles: log y-scale, EMA smoothing slider, normalization. */
export default function ChartControls({ opts, onChange, dropped = 0, axisMetrics }: Props) {
  return (
    <div className="flex flex-wrap items-center gap-2 mb-2">
      {axisMetrics && (
        <select
          value={opts.xAxis}
          onChange={(e) => onChange({ xAxis: e.target.value })}
          title="X-axis (log your own axis, e.g. env_steps or tokens_seen, as a metric)"
          style={{ ...pill(opts.xAxis !== 'step'), padding: '0 2px' }}
        >
          <option value="step">x: step</option>
          <option value="time">x: wall time</option>
          <option value="rel">x: elapsed</option>
          {axisMetrics.map((n) => (
            <option key={n} value={`metric:${n}`}>
              x: {n}
            </option>
          ))}
          {opts.xAxis.startsWith('metric:') && !axisMetrics.includes(opts.xAxis.slice(7)) && (
            <option value={opts.xAxis}>x: {opts.xAxis.slice(7)} (n/a)</option>
          )}
        </select>
      )}
      <button
        style={pill(opts.logY)}
        onClick={() => onChange({ logY: !opts.logY })}
        title="Logarithmic y-axis (values <= 0 are hidden)"
      >
        log
      </button>
      <select
        value={opts.norm}
        onChange={(e) => onChange({ norm: e.target.value as Normalization })}
        title="Normalization"
        style={{ ...pill(opts.norm !== 'none'), padding: '0 2px' }}
      >
        <option value="none">raw</option>
        <option value="first">÷ first</option>
        <option value="best">% of best</option>
      </select>
      <label
        className="flex items-center gap-1 text-xs font-mono"
        style={{ color: opts.alpha > 0 ? '#f59e0b' : '#4a4e60', fontSize: 10 }}
        title="EMA smoothing (shift+scroll also works)"
      >
        smooth
        <input
          type="range"
          min={0}
          max={MAX_ALPHA}
          step={0.01}
          value={opts.alpha}
          onChange={(e) => onChange({ alpha: parseFloat(e.target.value) })}
          style={{ width: 64 }}
        />
        {opts.alpha.toFixed(2)}
      </label>
      {opts.logY && dropped > 0 && (
        <span className="text-xs font-mono" style={{ color: '#f59e0b', fontSize: 10 }}>
          {dropped} value{dropped === 1 ? '' : 's'} ≤ 0 hidden
        </span>
      )}
    </div>
  )
}

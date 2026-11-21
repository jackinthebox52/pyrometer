import {
  MAX_ALPHA,
  resetChartDefaults,
  setChartDefaults,
  useChartDefaults,
} from '../utils/chartOptions.ts'
import type { Normalization } from '../utils/chartOptions.ts'

const sel: React.CSSProperties = {
  backgroundColor: '#1a1d24',
  color: '#c8cad6',
  border: '1px solid #353840',
  borderRadius: 4,
}

/**
 * Global chart defaults (log y, normalization, x-axis, smoothing). Every chart follows these until it is
 * changed individually; a chart's "reset" button returns it to these values.
 */
export default function ChartDefaults({ axisMetrics = [] }: { axisMetrics?: string[] }) {
  const d = useChartDefaults()
  return (
    <div
      className="flex flex-wrap items-center gap-2 px-3 py-2 rounded text-xs font-mono"
      style={{ backgroundColor: '#111318', border: '1px solid #252830', color: '#4a4e60' }}
      title="Defaults for every chart; individual charts can override them"
    >
      <span>Chart defaults</span>
      <select value={d.xAxis} onChange={(e) => setChartDefaults({ xAxis: e.target.value })} style={sel} title="Default x-axis">
        <option value="step">x: step</option>
        <option value="time">x: wall time</option>
        <option value="rel">x: elapsed</option>
        {axisMetrics.map((n) => (
          <option key={n} value={`metric:${n}`}>x: {n}</option>
        ))}
        {d.xAxis.startsWith('metric:') && !axisMetrics.includes(d.xAxis.slice(7)) && (
          <option value={d.xAxis}>x: {d.xAxis.slice(7)}</option>
        )}
      </select>
      <label className="flex items-center gap-1" style={{ color: d.logY ? '#f59e0b' : '#4a4e60' }}>
        <input type="checkbox" checked={d.logY} onChange={(e) => setChartDefaults({ logY: e.target.checked })} />
        log
      </label>
      <select
        value={d.norm}
        onChange={(e) => setChartDefaults({ norm: e.target.value as Normalization })}
        style={sel}
        title="Default normalization"
      >
        <option value="none">raw</option>
        <option value="first">÷ first</option>
        <option value="best">% of best</option>
      </select>
      <label className="flex items-center gap-1" style={{ color: d.alpha > 0 ? '#f59e0b' : '#4a4e60' }}>
        smooth
        <input
          type="range"
          min={0}
          max={MAX_ALPHA}
          step={0.01}
          value={d.alpha}
          onChange={(e) => setChartDefaults({ alpha: parseFloat(e.target.value) })}
          style={{ width: 80, accentColor: '#f59e0b' }}
        />
        {d.alpha.toFixed(2)}
      </label>
      <button onClick={resetChartDefaults} style={{ color: '#6b6f85' }} title="Restore built-in defaults">
        reset
      </button>
    </div>
  )
}

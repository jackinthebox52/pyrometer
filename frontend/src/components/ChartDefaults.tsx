import {
  MAX_ALPHA,
  resetChartDefaults,
  setChartDefaults,
  useChartDefaults,
} from '../utils/chartOptions.ts'
import type { Normalization } from '../utils/chartOptions.ts'
import { selectCls, toggleCls } from './ChartControls.tsx'

/**
 * Global chart defaults (x axis, log scale, normalization, smoothing). Every chart follows these until it is
 * changed individually; a chart's "Reset to defaults" button returns it to these values.
 */
export default function ChartDefaults({ axisMetrics = [] }: { axisMetrics?: string[] }) {
  const d = useChartDefaults()
  const field = 'flex items-center gap-2 text-xs text-py-muted'
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-sm border border-py-border bg-py-surface px-3 py-2 text-xs text-py-soft"
      title="Defaults for every chart; individual charts can override them. Drag a chart to zoom; Shift+scroll over it to smooth."
    >
      <span className="text-py-text">Chart defaults</span>
      <span className="hidden text-py-faint lg:inline">Drag to zoom. Shift+scroll to smooth.</span>
      <label className={field}>
        X axis
        <select value={d.xAxis} onChange={(e) => setChartDefaults({ xAxis: e.target.value })} className={selectCls}>
          <option value="step">Step</option>
          <option value="time">Wall time</option>
          <option value="rel">Elapsed</option>
          {axisMetrics.map((n) => (
            <option key={n} value={`metric:${n}`}>{n}</option>
          ))}
          {d.xAxis.startsWith('metric:') && !axisMetrics.includes(d.xAxis.slice(7)) && (
            <option value={d.xAxis}>{d.xAxis.slice(7)}</option>
          )}
        </select>
      </label>
      <span className={field}>
        Log scale
        <button
          type="button"
          aria-pressed={d.logY}
          onClick={() => setChartDefaults({ logY: !d.logY })}
          className={toggleCls(d.logY)}
        >
          {d.logY ? 'On' : 'Off'}
        </button>
      </span>
      <label className={field}>
        Normalize
        <select
          value={d.norm}
          onChange={(e) => setChartDefaults({ norm: e.target.value as Normalization })}
          className={selectCls}
        >
          <option value="none">Raw values</option>
          <option value="first">Divide by first</option>
          <option value="best">Percent of best</option>
        </select>
      </label>
      <label className={field}>
        Smoothing
        <input
          type="range"
          min={0}
          max={MAX_ALPHA}
          step={0.01}
          value={d.alpha}
          onChange={(e) => setChartDefaults({ alpha: parseFloat(e.target.value) })}
          className="w-20"
        />
        <span className="num w-8 text-py-soft">{d.alpha.toFixed(2)}</span>
      </label>
      <button
        type="button"
        onClick={resetChartDefaults}
        title="Restore built-in defaults"
        className={`${toggleCls(false)} ml-auto`}
      >
        Reset
      </button>
    </div>
  )
}

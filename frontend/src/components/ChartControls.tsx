import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import Icon from './Icon.tsx'
import { BUILTIN_OPTIONS, MAX_ALPHA } from '../utils/chartOptions.ts'
import { coerceAxis } from '../utils/xAxis.ts'
import type { ChartOptions, Normalization } from '../utils/chartOptions.ts'

interface Props {
  opts: ChartOptions
  onChange: (patch: Partial<ChartOptions>) => void
  /** Number of points hidden because log scale cannot show values <= 0. */
  dropped?: number
  /** Metric names usable as x-axis. When undefined, the x-axis selector is hidden. */
  axisMetrics?: string[]
  /** Base x-axis kinds to offer (default step, time, rel). GPU charts have no steps: ['rel', 'time']. */
  axes?: readonly string[]
  /** True when this chart overrides the global defaults; shows a reset button. */
  overridden?: boolean
  onReset?: () => void
}

/** Shared control classes (also used by the global defaults bar). */
export const selectCls =
  'h-7 rounded-sm border border-py-border-hi bg-py-surface px-2 text-xs text-py-soft transition-colors duration-150 ease-out hover:bg-py-elevated hover:text-py-text'
export const toggleCls = (on: boolean): string =>
  `h-7 rounded-sm border px-2.5 text-xs transition-colors duration-150 ease-out ${
    on
      ? 'border-py-n8 bg-py-elevated text-py-text'
      : 'border-py-border-hi bg-py-surface text-py-soft hover:bg-py-elevated hover:text-py-text'
  }`

/** Base x-axis choices shown in a select. */
function axisLabelOf(kind: string): string {
  if (kind === 'step') return 'Step'
  if (kind === 'time') return 'Wall time'
  if (kind === 'rel') return 'Elapsed'
  return kind
}

const AXIS_CHIP: Record<string, string> = { step: 'step', time: 'time', rel: 'elapsed' }

/** Short labels for every setting that differs from the built-in defaults, so chart state stays visible. */
export function chartChips(opts: ChartOptions): string[] {
  const chips: string[] = []
  if (opts.xAxis !== BUILTIN_OPTIONS.xAxis) chips.push(`x: ${AXIS_CHIP[opts.xAxis] ?? opts.xAxis.replace(/^metric:/, '')}`)
  if (opts.logY) chips.push('log')
  if (opts.norm !== 'none') chips.push(`norm: ${opts.norm}`)
  if (opts.alpha > 0) chips.push(`ema ${opts.alpha.toFixed(2).replace(/0$/, '')}`)
  return chips
}

/** Tiny mono chips for a chart's non-default settings. */
export function ChartChips({ opts }: { opts: ChartOptions }) {
  const chips = chartChips(opts)
  if (chips.length === 0) return null
  return (
    <span className="flex flex-wrap items-center gap-1">
      {chips.map((c) => (
        <span key={c} className="num rounded-sm border border-py-border px-1 text-[10px] leading-4 text-py-muted">
          {c}
        </span>
      ))}
    </span>
  )
}

const POPOVER_W = 264

/**
 * Per-chart settings: a small icon button for the chart header that opens a popover with the x-axis, log scale,
 * normalization and EMA smoothing controls. The button shows on hover/focus of the panel (the parent needs the
 * `group` class) and always on touch. Escape and outside click close it.
 */
export default function ChartControls({ opts, onChange, dropped = 0, axisMetrics, axes, overridden = false, onReset }: Props) {
  const kinds = axes ?? ['step', 'time', 'rel']
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const id = useId()

  const place = useCallback(() => {
    const r = btnRef.current?.getBoundingClientRect()
    if (!r) return
    const left = Math.max(8, Math.min(window.innerWidth - POPOVER_W - 8, r.right - POPOVER_W))
    setPos({ top: r.bottom + 4, left })
  }, [])

  useLayoutEffect(() => {
    if (open) place()
  }, [open, place])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        setOpen(false)
        btnRef.current?.focus()
      }
    }
    const onDown = (e: MouseEvent | TouchEvent) => {
      const t = e.target as Node
      if (panelRef.current?.contains(t) || btnRef.current?.contains(t)) return
      setOpen(false)
    }
    document.addEventListener('keydown', onKey, true)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('touchstart', onDown)
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    panelRef.current?.querySelector<HTMLElement>('select, button, input')?.focus()
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('touchstart', onDown)
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open, place])

  const field = 'flex flex-col gap-1'
  return (
    <>
      <button
        ref={btnRef}
        type="button"
        aria-label="Chart settings"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        title="Chart settings"
        onClick={() => setOpen((v) => !v)}
        className={`inline-flex h-6 w-6 items-center justify-center rounded-sm text-py-muted transition-colors duration-150 ease-out hover:bg-py-elevated hover:text-py-text focus-visible:opacity-100 group-focus-within:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100 ${
          open ? 'bg-py-elevated text-py-text opacity-100' : 'opacity-0'
        }`}
      >
        <Icon name="more" size={16} />
      </button>
      {open &&
        pos &&
        createPortal(
          <div
            ref={panelRef}
            id={id}
            role="dialog"
            aria-label="Chart settings"
            style={{ position: 'fixed', top: pos.top, left: pos.left, width: POPOVER_W }}
            className="z-[300] flex flex-col gap-3 rounded-sm border border-py-border-hi bg-py-elevated p-3 text-xs text-py-soft"
          >
            {axisMetrics && (
              <label className={field}>
                <span className="log-label">X axis</span>
                <select
                  value={coerceAxis(opts.xAxis, axes)}
                  onChange={(e) => onChange({ xAxis: e.target.value })}
                  title="Log your own axis, e.g. env_steps or tokens_seen, as a metric"
                  className={selectCls}
                >
                  {kinds.map((k) => (
                    <option key={k} value={k}>
                      {axisLabelOf(k)}
                    </option>
                  ))}
                  {axisMetrics.map((n) => (
                    <option key={n} value={`metric:${n}`}>
                      {n}
                    </option>
                  ))}
                  {opts.xAxis.startsWith('metric:') && !axisMetrics.includes(opts.xAxis.slice(7)) && (
                    <option value={opts.xAxis}>{opts.xAxis.slice(7)} (not logged)</option>
                  )}
                </select>
              </label>
            )}
            <div className={field}>
              <span className="log-label">Log scale</span>
              <button
                type="button"
                aria-pressed={opts.logY}
                onClick={() => onChange({ logY: !opts.logY })}
                title="Logarithmic y-axis (values <= 0 are hidden)"
                className={toggleCls(opts.logY)}
              >
                {opts.logY ? 'On' : 'Off'}
              </button>
              {opts.logY && dropped > 0 && (
                <span className="text-py-muted">
                  {dropped} value{dropped === 1 ? '' : 's'} at or below 0 hidden
                </span>
              )}
            </div>
            <label className={field}>
              <span className="log-label">Normalize</span>
              <select
                value={opts.norm}
                onChange={(e) => onChange({ norm: e.target.value as Normalization })}
                className={selectCls}
              >
                <option value="none">Raw values</option>
                <option value="first">Divide by first</option>
                <option value="best">Percent of best</option>
              </select>
            </label>
            <label className={field}>
              <span className="log-label flex justify-between">
                <span>Smoothing</span>
                <span className="num text-py-soft">{opts.alpha.toFixed(2)}</span>
              </span>
              <input
                type="range"
                min={0}
                max={MAX_ALPHA}
                step={0.01}
                value={opts.alpha}
                onChange={(e) => onChange({ alpha: parseFloat(e.target.value) })}
                title="EMA smoothing (shift+scroll over the chart also works)"
                className="h-7 w-full"
              />
            </label>
            {overridden && onReset && (
              <button
                type="button"
                onClick={onReset}
                title="Reset this chart to the global chart defaults"
                className={toggleCls(false)}
              >
                Reset to defaults
              </button>
            )}
          </div>,
          document.body,
        )}
    </>
  )
}

import { useEffect, useId, useRef, useState } from 'react'
import Icon from './Icon.tsx'
import { matchesPattern, movePin } from '../utils/viewConfig.ts'
import type { ViewConfig } from '../utils/viewConfig.ts'

const btn =
  'inline-flex h-7 items-center gap-1.5 rounded border border-py-border-hi bg-py-surface px-2.5 text-[13px] text-py-soft transition-colors hover:bg-py-elevated hover:text-py-text disabled:cursor-not-allowed disabled:opacity-50'
const iconBtn =
  'inline-flex h-6 w-6 items-center justify-center rounded text-py-muted transition-colors hover:bg-py-elevated hover:text-py-text disabled:cursor-not-allowed disabled:opacity-40'

interface Props {
  project: string
  cfg: ViewConfig
  /** Tabs that can be hidden (everything but Overview). */
  tabs: { id: string; label: string }[]
  /** Effective pinned list (the defaults when never customised). */
  pinned: string[]
  metricNames: string[]
  onTabHidden: (id: string, hidden: boolean) => void
  onPinsChange: (pins: string[]) => void
  onShow: (pattern: string) => void
  onReset: () => void
}

/**
 * "Customize view": a popover at the end of the tab bar. Everything here applies to every run of the project, on this
 * browser (the config lives in localStorage, see utils/viewConfig.ts).
 */
export default function ViewSettings({ project, cfg, tabs, pinned, metricNames, onTabHidden, onPinsChange, onShow, onReset }: Props) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const titleId = useId()

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', esc)
    }
  }, [open])

  return (
    <div className="relative" ref={rootRef}>
      <button type="button" className={`${btn} max-sm:w-7 max-sm:justify-center max-sm:px-0`} aria-label="Customize view" title="Customize view" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen((v) => !v)}>
        <Icon name="settings" size={14} />
        <span className="max-sm:hidden">Customize view</span>
      </button>
      {open && (
        <div
          role="dialog"
          aria-labelledby={titleId}
          className="absolute right-0 z-30 mt-1 max-h-[70vh] w-[min(360px,calc(100vw-32px))] overflow-y-auto rounded border border-py-border-hi bg-py-surface p-3"
        >
          <div className="mb-1 flex items-start justify-between gap-2">
            <h2 id={titleId} className="text-[15px] font-semibold text-py-text">Customize view</h2>
            <button type="button" className={iconBtn} aria-label="Close" onClick={() => setOpen(false)}>
              <Icon name="x" size={14} />
            </button>
          </div>
          <p className="mb-3 text-xs text-py-muted">
            Applies to every run in project <span className="num text-py-soft">{project}</span>, on this browser.
          </p>

          <fieldset className="mb-4">
            <legend className="log-label mb-1.5">Tabs</legend>
            <div className="grid grid-cols-1 gap-0.5">
              <label className="flex items-center gap-2 py-0.5 text-[13px] text-py-faint">
                <input type="checkbox" checked disabled /> Overview <span className="text-xs">always shown</span>
              </label>
              {tabs.map((t) => (
                <label key={t.id} className="flex cursor-pointer items-center gap-2 py-0.5 text-[13px] text-py-soft">
                  <input type="checkbox" checked={!cfg.hiddenTabs.includes(t.id)} onChange={(e) => onTabHidden(t.id, !e.target.checked)} />
                  {t.label}
                </label>
              ))}
            </div>
            <p className="mt-1 text-xs text-py-muted">A tab with no data for a run is hidden anyway.</p>
          </fieldset>

          <section className="mb-4" aria-label="Pinned metrics">
            <h3 className="log-label mb-1.5">Pinned on Overview</h3>
            {pinned.length === 0 ? (
              <p className="text-xs text-py-muted">Nothing pinned. Use the pin button on a chart in the Metrics tab.</p>
            ) : (
              <ul className="flex flex-col gap-0.5">
                {pinned.map((p, i) => (
                  <li key={p} className="flex items-center gap-1 rounded border border-py-border px-2 py-0.5">
                    <span className="num min-w-0 flex-1 truncate text-xs text-py-soft" title={p}>{p}</span>
                    <button type="button" className={`${iconBtn} rotate-180`} aria-label={`Move ${p} up`} disabled={i === 0} onClick={() => onPinsChange(movePin(pinned, i, -1))}>
                      <Icon name="chevron-down" size={12} />
                    </button>
                    <button type="button" className={iconBtn} aria-label={`Move ${p} down`} disabled={i === pinned.length - 1} onClick={() => onPinsChange(movePin(pinned, i, 1))}>
                      <Icon name="chevron-down" size={12} />
                    </button>
                    <button type="button" className={iconBtn} aria-label={`Unpin ${p}`} onClick={() => onPinsChange(pinned.filter((x) => x !== p))}>
                      <Icon name="x" size={12} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="mb-4" aria-label="Hidden metrics">
            <h3 className="log-label mb-1.5">Hidden metrics</h3>
            {cfg.hidden.length === 0 ? (
              <p className="text-xs text-py-muted">None. Use the hide button on a chart to keep it out of the way.</p>
            ) : (
              <ul className="flex flex-col gap-0.5">
                {cfg.hidden.map((p) => {
                  const n = metricNames.filter((m) => matchesPattern(m, p)).length
                  return (
                    <li key={p} className="flex items-center gap-2 rounded border border-py-border px-2 py-0.5">
                      <span className="num min-w-0 flex-1 truncate text-xs text-py-soft" title={p}>{p}</span>
                      {p.includes('*') && <span className="text-xs text-py-muted">{n} here</span>}
                      <button type="button" className="text-xs text-py-soft underline hover:text-py-text" aria-label={`Show ${p}`} onClick={() => onShow(p)}>
                        Show
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>

          <button type="button" className={btn} onClick={onReset}>Reset to defaults</button>
        </div>
      )}
    </div>
  )
}

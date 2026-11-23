import { useMemo, useState } from 'react'
import type { Metric, RunEvent } from '../../api/types.ts'
import type { XSource } from '../../utils/xAxis.ts'
import type { RangeQuery } from '../../utils/series.ts'
import { isHidden, matchesPattern } from '../../utils/viewConfig.ts'
import type { ViewConfig } from '../../utils/viewConfig.ts'
import ChartDefaults from '../ChartDefaults.tsx'
import Icon from '../Icon.tsx'
import MetricChart from '../MetricChart.tsx'
import type { ExternalZoom } from '../MetricChart.tsx'
import { btn, CHART_GRID, ChartCell, iconBtn, Note } from './shared.tsx'

export interface MetricRange {
  tMin: number
  tMax: number
}

interface Props {
  metricNames: string[]
  metricsByName: Record<string, Metric[]>
  events: RunEvent[]
  startTime: number
  cfg: ViewConfig
  axisSource: XSource
  fetchRange: (name: string, q: RangeQuery, maxPoints: number) => Promise<Metric[]>
  liveTick: number
  showEvents: boolean
  onShowEvents: (v: boolean) => void
  isPinned: (name: string) => boolean
  onTogglePin: (name: string) => void
  onHide: (pattern: string) => void
  onShow: (pattern: string) => void
  /** A time window chosen on the GPU load curve (or by drag-zooming here); null = no window. */
  range: MetricRange | null
  rangeByName: Record<string, Metric[]> | null
  rangeZoom: ExternalZoom
}

const GENERAL = ''

function groupOf(name: string): string {
  const i = name.indexOf('/')
  return i > 0 ? name.slice(0, i) : GENERAL
}

function fmtElapsed(secs: number): string {
  const s = Math.max(0, Math.round(secs))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}h ${String(m).padStart(2, '0')}m` : m > 0 ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`
}

export default function MetricsTab(p: Props) {
  const [filter, setFilter] = useState('')
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [hiddenOpen, setHiddenOpen] = useState(false)

  const q = filter.trim().toLowerCase()
  const visibleNames = useMemo(() => p.metricNames.filter((n) => !isHidden(n, p.cfg)), [p.metricNames, p.cfg])
  const hiddenNames = useMemo(() => p.metricNames.filter((n) => isHidden(n, p.cfg)), [p.metricNames, p.cfg])
  const matching = useMemo(() => visibleNames.filter((n) => !q || n.toLowerCase().includes(q)), [visibleNames, q])

  const groups = useMemo(() => {
    const m = new Map<string, string[]>()
    for (const n of matching) {
      const g = groupOf(n)
      m.set(g, [...(m.get(g) ?? []), n])
    }
    // `auto` first, then groups alphabetically, and the catch-all "Other" (no prefix) last.
    const rank = (g: string) => (g === 'auto' ? 0 : g === GENERAL ? 2 : 1)
    return [...m.entries()].sort(([a], [b]) => rank(a) - rank(b) || (a < b ? -1 : a > b ? 1 : 0))
  }, [matching])

  const origin = Math.min(p.startTime, p.range?.tMin ?? p.startTime)
  const dataFor = (name: string): Metric[] => (p.rangeByName ? p.rangeByName[name] ?? [] : p.metricsByName[name])

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <label className="relative">
          <span className="sr-only">Filter metrics</span>
          <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-py-muted"><Icon name="search" size={14} /></span>
          <input
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter metrics…"
            className="h-8 w-56 rounded border border-py-border-hi bg-py-elevated pl-7 pr-2 text-[13px] text-py-text placeholder:text-py-faint"
          />
        </label>
        <button type="button" className={btn} aria-pressed={p.showEvents} onClick={() => p.onShowEvents(!p.showEvents)}>
          <Icon name={p.showEvents ? 'eye' : 'eye-off'} size={14} />
          Events {p.showEvents ? 'shown' : 'hidden'}
        </button>
        <ChartDefaults axisMetrics={p.metricNames} />
      </div>

      {p.range && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded border border-py-border-hi bg-py-surface px-3 py-2 text-[13px] text-py-soft">
          <span>
            Zoomed to <span className="num text-py-text">{fmtElapsed(p.range.tMin - origin)} to {fmtElapsed(p.range.tMax - origin)}</span> of
            the run. Drag on a chart to narrow it further.
          </span>
          <button type="button" className={btn} onClick={p.rangeZoom.onReset}>Reset zoom</button>
        </div>
      )}

      {matching.length === 0 && (
        <Note>{q ? `No visible metric matches “${filter.trim()}”.` : 'Every metric is hidden. Show them again under Hidden below.'}</Note>
      )}

      <div className="flex flex-col gap-6">
        {groups.map(([group, names]) => {
          const isCollapsed = !q && collapsed[group]
          const label = group === GENERAL ? 'Other' : group
          const panelId = `metric-group-${group || 'other'}`
          return (
            <section key={group || 'other'} aria-label={`${label} metrics`}>
              <div className="mb-2 flex items-center gap-2">
                <button
                  type="button"
                  className="flex items-center gap-1.5 text-[13px] font-semibold text-py-text"
                  aria-expanded={!isCollapsed}
                  aria-controls={panelId}
                  onClick={() => setCollapsed((c) => ({ ...c, [group]: !c[group] }))}
                >
                  <Icon name={isCollapsed ? 'chevron-right' : 'chevron-down'} size={12} />
                  {label}
                  <span className="num text-[11px] font-normal text-py-muted">{names.length}</span>
                </button>
                {group !== GENERAL && (
                  <button
                    type="button"
                    className={`${iconBtn} border-transparent bg-transparent`}
                    aria-label={`Hide every ${group}/ metric`}
                    title={`Hide ${group}/*`}
                    onClick={() => p.onHide(`${group}/*`)}
                  >
                    <Icon name="eye-off" size={13} />
                  </button>
                )}
              </div>
              {!isCollapsed && (
                <div id={panelId} className={CHART_GRID}>
                  {names.map((name) => (
                    <ChartCell key={name} name={name} pinned={p.isPinned(name)} onTogglePin={() => p.onTogglePin(name)} onHide={() => p.onHide(name)}>
                      <MetricChart
                        name={name}
                        data={dataFor(name)}
                        events={p.showEvents ? p.events : []}
                        source={p.axisSource}
                        fetchRange={p.fetchRange}
                        refreshKey={p.liveTick}
                        zoom={p.range ? p.rangeZoom : undefined}
                      />
                    </ChartCell>
                  ))}
                </div>
              )}
            </section>
          )
        })}
      </div>

      {p.cfg.hidden.length > 0 && (
        <section className="mt-8 rounded border border-py-border" aria-label="Hidden metrics">
          <button
            type="button"
            className="flex w-full items-center gap-1.5 px-3 py-2 text-left text-[13px] text-py-soft hover:text-py-text"
            aria-expanded={hiddenOpen}
            onClick={() => setHiddenOpen((v) => !v)}
          >
            <Icon name={hiddenOpen ? 'chevron-down' : 'chevron-right'} size={12} />
            Hidden <span className="num text-[11px] text-py-muted">({hiddenNames.length})</span>
          </button>
          {hiddenOpen && (
            <ul className="divide-y divide-py-border border-t border-py-border">
              {p.cfg.hidden.map((pat) => (
                <li key={pat} className="flex items-center gap-3 px-3 py-1.5">
                  <span className="num min-w-0 flex-1 truncate text-xs text-py-soft">{pat}</span>
                  {pat.includes('*') && (
                    <span className="text-xs text-py-muted">{p.metricNames.filter((n) => matchesPattern(n, pat)).length} here</span>
                  )}
                  <button type="button" className={btn} aria-label={`Show ${pat}`} onClick={() => p.onShow(pat)}>Show</button>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  )
}

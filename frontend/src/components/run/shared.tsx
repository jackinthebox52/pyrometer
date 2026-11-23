import type { ReactNode } from 'react'
import Icon from '../Icon.tsx'

export const btn =
  'inline-flex h-7 items-center gap-1.5 rounded border border-py-border-hi bg-py-surface px-3 text-[13px] text-py-soft transition-colors hover:bg-py-elevated hover:text-py-text disabled:cursor-not-allowed disabled:opacity-50'
export const btnPrimary =
  'inline-flex h-7 items-center gap-1.5 rounded bg-py-soft px-3 text-[13px] font-medium text-py-base transition-colors hover:bg-py-text disabled:cursor-not-allowed disabled:opacity-50'
export const iconBtn =
  'inline-flex h-6 w-6 items-center justify-center rounded border border-py-border-hi bg-py-surface text-py-muted transition-colors hover:bg-py-elevated hover:text-py-text'

export type TabId = 'overview' | 'metrics' | 'system' | 'health' | 'profiler' | 'logs' | 'media' | 'artifacts' | 'notes'

export const TAB_LABEL: Record<TabId, string> = {
  overview: 'Overview',
  metrics: 'Metrics',
  system: 'System',
  health: 'Model health',
  profiler: 'Profiler',
  logs: 'Logs',
  media: 'Media & samples',
  artifacts: 'Artifacts',
  notes: 'Notes & config',
}
export const TAB_ORDER = Object.keys(TAB_LABEL) as TabId[]

export function Heading({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-3 flex items-center justify-between gap-3">
      <h2 className="text-[15px] font-semibold text-py-text">{children}</h2>
      {right}
    </div>
  )
}

export function Skeleton({ h = 16, w = '100%' }: { h?: number; w?: string | number }) {
  return <div className="shrink-0 animate-pulse rounded bg-py-elevated" style={{ height: h, width: w }} />
}

/** Hairline-bordered box for plain text states ("no data" notes). */
export function Note({ children }: { children: ReactNode }) {
  return <div className="rounded border border-py-border bg-py-surface p-4 text-[13px] text-py-muted">{children}</div>
}

/**
 * Wraps a chart (whose own title and controls fill its top row) with pin / hide buttons at its bottom-left, shown on hover and
 * keyboard focus (always on touch devices).
 */
export function ChartCell({
  name,
  pinned,
  onTogglePin,
  onHide,
  children,
}: {
  name: string
  pinned: boolean
  onTogglePin: () => void
  onHide?: () => void
  children: ReactNode
}) {
  return (
    <div className="group relative min-w-0">
      <div className="absolute bottom-1 left-2 z-10 flex gap-1 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100">
        <button
          type="button"
          className={`${iconBtn} ${pinned ? 'text-py-text' : ''}`}
          aria-label={pinned ? `Unpin ${name} from Overview` : `Pin ${name} to Overview`}
          aria-pressed={pinned}
          title={pinned ? 'Unpin from Overview' : 'Pin to Overview'}
          onClick={onTogglePin}
        >
          <Icon name="pin" size={13} />
        </button>
        {onHide && (
          <button type="button" className={iconBtn} aria-label={`Hide ${name}`} title="Hide this chart (restore it under Hidden)" onClick={onHide}>
            <Icon name="eye-off" size={13} />
          </button>
        )}
      </div>
      {children}
    </div>
  )
}

export const CHART_GRID = 'grid grid-cols-1 gap-4 lg:grid-cols-2'

/** Copy to the clipboard, falling back to a hidden textarea where the async API needs a secure context (plain-HTTP LAN). */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

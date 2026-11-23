import { useRef } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'
import { TAB_LABEL } from './shared.tsx'
import type { TabId } from './shared.tsx'

interface Props {
  tabs: TabId[]
  active: TabId
  badges?: Partial<Record<TabId, number>>
  onChange: (id: TabId) => void
  /** Right end of the bar (the Customize view button). */
  right?: ReactNode
}

export const tabDomId = (id: TabId) => `run-tab-${id}`
export const panelDomId = (id: TabId) => `run-panel-${id}`

/** Standard tabs: roving tabindex, arrow keys / Home / End move and activate. */
export default function RunTabs({ tabs, active, badges, onChange, right }: Props) {
  const refs = useRef<Partial<Record<TabId, HTMLButtonElement | null>>>({})

  function onKey(e: KeyboardEvent) {
    const i = tabs.indexOf(active)
    let next = -1
    if (e.key === 'ArrowRight') next = (i + 1) % tabs.length
    else if (e.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = tabs.length - 1
    if (next < 0) return
    e.preventDefault()
    onChange(tabs[next])
    refs.current[tabs[next]]?.focus()
  }

  return (
    <div className="mb-5 flex items-end justify-between gap-3 border-b border-py-border">
      <div role="tablist" aria-label="Run sections" className="-mb-px flex min-w-0 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden" onKeyDown={onKey}>
        {tabs.map((id) => {
          const on = id === active
          const badge = badges?.[id]
          return (
            <button
              key={id}
              ref={(el) => {
                refs.current[id] = el
              }}
              type="button"
              role="tab"
              id={tabDomId(id)}
              aria-selected={on}
              aria-controls={panelDomId(id)}
              tabIndex={on ? 0 : -1}
              onClick={() => onChange(id)}
              className={`h-9 shrink-0 whitespace-nowrap border-b-2 px-3 text-[13px] transition-colors ${
                on ? 'border-py-soft text-py-text' : 'border-transparent text-py-muted hover:text-py-soft'
              }`}
            >
              {TAB_LABEL[id]}
              {badge != null && badge > 0 && <span className="num ml-1.5 text-[11px] text-py-red">{badge}</span>}
            </button>
          )
        })}
      </div>
      <div className="mb-1 shrink-0">{right}</div>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { HashRouter, Route, Routes, useLocation } from 'react-router-dom'
import { decodeIds } from './utils/compareState.ts'
import LoginGate from './components/LoginGate.tsx'
import Icon from './components/Icon.tsx'
import RunSidebar from './components/RunSidebar.tsx'
import CompareView from './pages/CompareView.tsx'
import EmptyState from './pages/EmptyState.tsx'
import RunDetailPage from './pages/RunDetailPage.tsx'
import SettingsPage from './pages/SettingsPage.tsx'

/** Opening or navigating (back/forward) to a compare URL makes its run ids the sidebar selection. */
function SelectionFromUrl({ onIds }: { onIds: (ids: string[]) => void }) {
  const { pathname, search } = useLocation()
  const ids = pathname === '/compare' ? decodeIds(new URLSearchParams(search)).join(',') : null
  useEffect(() => {
    if (ids) onIds(ids.split(','))
    // only a change of the URL's ids replaces the selection, not the user's later sidebar edits
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids])
  return null
}

export default function App() {
  const [selectedIds, setSelectedIds] = useState<string[]>([])

  function toggleSelect(id: string) {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    )
  }

  function selectMany(ids: string[], on: boolean) {
    setSelectedIds((prev) => {
      if (!on) return prev.filter((x) => !ids.includes(x))
      return [...prev, ...ids.filter((x) => !prev.includes(x))]
    })
  }

  function clearSelection() {
    setSelectedIds([])
  }

  return (
    <LoginGate>
    <HashRouter>
      <SelectionFromUrl onIds={setSelectedIds} />
      <Shell
        selectedIds={selectedIds}
        onToggleSelect={toggleSelect}
        onSelectMany={selectMany}
        onClearSelection={clearSelection}
      />
    </HashRouter>
    </LoginGate>
  )
}

interface ShellProps {
  selectedIds: string[]
  onToggleSelect: (id: string) => void
  onSelectMany: (ids: string[], on: boolean) => void
  onClearSelection: () => void
}

/**
 * Page chrome. From the md breakpoint up the run sidebar is a fixed column; below it the sidebar is a drawer opened from a
 * top bar (Escape, the backdrop or navigating closes it), so a phone-width page is the content, not a 288px sidebar.
 */
function Shell({ selectedIds, onToggleSelect, onSelectMany, onClearSelection }: ShellProps) {
  const [navOpen, setNavOpen] = useState(false)
  const { pathname, search } = useLocation()
  useEffect(() => setNavOpen(false), [pathname, search])
  useEffect(() => {
    if (!navOpen) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setNavOpen(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navOpen])

  return (
    <div className="flex h-screen overflow-hidden bg-py-base">
      {navOpen && <div className="fixed inset-0 z-30 bg-py-n0/80 md:hidden" onClick={() => setNavOpen(false)} aria-hidden="true" />}
      <div
        id="run-nav"
        className={`z-40 flex max-md:fixed max-md:inset-y-0 max-md:left-0 ${navOpen ? '' : 'max-md:hidden'}`}
      >
        <RunSidebar
          selectedIds={selectedIds}
          onToggleSelect={onToggleSelect}
          onSelectMany={onSelectMany}
          onClearSelection={onClearSelection}
        />
      </div>
      <main className="min-w-0 flex-1 overflow-y-auto">
        <div className="sticky top-0 z-20 flex items-center gap-3 border-b border-py-border bg-py-n0 px-4 py-2 md:hidden">
          <button
            onClick={() => setNavOpen(true)}
            aria-label="Open runs menu"
            aria-expanded={navOpen}
            aria-controls="run-nav"
            className="flex h-8 items-center gap-1.5 rounded-[2px] border border-py-border-hi bg-py-surface px-2.5 text-xs text-py-soft transition-colors duration-150 hover:bg-py-elevated hover:text-py-text"
          >
            <Icon name="menu" size={14} />
            Runs{selectedIds.length > 0 && <span className="num"> ({selectedIds.length})</span>}
          </button>
          <span className="text-[15px] font-semibold text-py-text">Pyrometer</span>
        </div>
        <Routes>
          <Route path="/" element={<EmptyState />} />
          <Route path="/runs/:runId" element={<RunDetailPage />} />
          <Route path="/compare" element={<CompareView />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
    </div>
  )
}

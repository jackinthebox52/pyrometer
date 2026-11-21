import { useEffect, useState } from 'react'
import { HashRouter, Route, Routes, useLocation } from 'react-router-dom'
import { decodeIds } from './utils/compareState.ts'
import LoginGate from './components/LoginGate.tsx'
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
      <div className="flex h-screen overflow-hidden bg-py-base">
        <RunSidebar
          selectedIds={selectedIds}
          onToggleSelect={toggleSelect}
          onSelectMany={selectMany}
          onClearSelection={clearSelection}
        />
        <main className="flex-1 overflow-y-auto">
          <Routes>
            <Route path="/" element={<EmptyState />} />
            <Route path="/runs/:runId" element={<RunDetailPage />} />
            <Route path="/compare" element={<CompareView />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </main>
      </div>
    </HashRouter>
    </LoginGate>
  )
}

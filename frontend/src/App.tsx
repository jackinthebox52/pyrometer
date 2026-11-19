import { useState } from 'react'
import { HashRouter, Route, Routes } from 'react-router-dom'
import LoginGate from './components/LoginGate.tsx'
import RunSidebar from './components/RunSidebar.tsx'
import CompareView from './pages/CompareView.tsx'
import EmptyState from './pages/EmptyState.tsx'
import RunDetailPage from './pages/RunDetailPage.tsx'

export default function App() {
  const [selectedIds, setSelectedIds] = useState<string[]>([])

  function toggleSelect(id: string) {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    )
  }

  function clearSelection() {
    setSelectedIds([])
  }

  return (
    <LoginGate>
    <HashRouter>
      <div className="flex h-screen overflow-hidden bg-py-base">
        <RunSidebar
          selectedIds={selectedIds}
          onToggleSelect={toggleSelect}
          onClearSelection={clearSelection}
        />
        <main className="flex-1 overflow-y-auto">
          <Routes>
            <Route path="/" element={<EmptyState />} />
            <Route path="/runs/:runId" element={<RunDetailPage />} />
            <Route path="/compare" element={<CompareView />} />
          </Routes>
        </main>
      </div>
    </HashRouter>
    </LoginGate>
  )
}

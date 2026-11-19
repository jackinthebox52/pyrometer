import { useEffect, useRef, useState } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { api } from '../api/client.ts'
import type { Run } from '../api/types.ts'
import { timeAgo } from '../utils/format.ts'
import { RUN_COLORS } from '../pages/CompareView.tsx'
import { useWebSocket } from '../hooks/useWebSocket.ts'

const STATUS_COLOR: Record<string, string> = {
  running: '#22c55e',
  completed: '#60a5fa',
  crashed: '#ef4444',
  killed: '#eab308',
}

interface Props {
  selectedIds: string[]
  onToggleSelect: (id: string) => void
  onClearSelection: () => void
}

export default function RunSidebar({ selectedIds, onToggleSelect, onClearSelection }: Props) {
  const [runs, setRuns] = useState<Run[]>([])
  const [query, setQuery] = useState('')
  const [serverDown, setServerDown] = useState(false)
  const navigate = useNavigate()
  const loadRef = useRef<() => void>()

  useEffect(() => {
    let mounted = true
    let interval: ReturnType<typeof setInterval> | null = null

    async function load() {
      try {
        const data = await api.listRuns()
        if (!mounted) return
        const sorted = [...data].sort(
          (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
        )
        setRuns(sorted)
        setServerDown(false)

        // Only poll while there are active runs — WS events cover all other cases
        const hasRunning = sorted.some((r) => r.status === 'running')
        if (hasRunning && !interval) {
          interval = setInterval(load, 10000)
        } else if (!hasRunning && interval) {
          clearInterval(interval)
          interval = null
        }
      } catch {
        if (mounted) setServerDown(true)
      }
    }

    loadRef.current = load
    load()
    return () => {
      mounted = false
      if (interval) clearInterval(interval)
    }
  }, [])

  // Refresh immediately when any run is created or its status changes
  useWebSocket((msg) => {
    if (msg.type === 'run_created' || msg.type === 'run_updated') {
      loadRef.current?.()
    }
  })

  const filtered = runs.filter((r) => {
    if (!query) return true
    const q = query.toLowerCase()
    return r.name.toLowerCase().includes(q) || r.tags.some((t) => t.toLowerCase().includes(q))
  })

  const selectedSet = new Set(selectedIds)
  const isComparing = selectedIds.length >= 2

  function handleCompare() {
    navigate(`/compare?ids=${selectedIds.join(',')}`)
  }

  return (
    <aside
      className="flex flex-col border-r"
      style={{ width: 256, minWidth: 256, backgroundColor: '#111318', borderColor: '#252830' }}
    >
      {/* Logo */}
      <div
        className="flex items-center gap-2.5 px-4 py-[14px] border-b"
        style={{ borderColor: '#252830' }}
      >
        <span style={{ color: '#f59e0b', fontSize: 17, lineHeight: 1 }}>◉</span>
        <span
          className="font-semibold tracking-widest text-xs uppercase"
          style={{ color: '#e8eaf0', letterSpacing: '0.18em' }}
        >
          Pyrometer
        </span>
      </div>

      {/* Search */}
      <div className="px-3 py-2.5 border-b" style={{ borderColor: '#252830' }}>
        <input
          type="text"
          placeholder="Filter by name or tag…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="w-full px-3 py-1.5 rounded text-xs outline-none placeholder-py-faint"
          style={{
            backgroundColor: '#0a0b0e',
            border: '1px solid #252830',
            color: '#e8eaf0',
            fontFamily: 'Sora, sans-serif',
          }}
        />
      </div>

      {/* Run list */}
      <div className="flex-1 overflow-y-auto py-1">
        {serverDown && (
          <div className="px-4 py-3 text-xs font-mono" style={{ color: '#4a4e60' }}>
            ⚠ Server unreachable
          </div>
        )}
        {!serverDown && filtered.length === 0 && (
          <div className="px-4 py-4 text-xs" style={{ color: '#4a4e60' }}>
            {query ? 'No matching runs' : 'No runs yet'}
          </div>
        )}
        {filtered.map((run) => {
          const isSelected = selectedSet.has(run.run_id)
          const selectionIndex = selectedIds.indexOf(run.run_id)
          const selectionColor =
            selectionIndex >= 0 ? RUN_COLORS[selectionIndex % RUN_COLORS.length] : undefined

          return (
            <div
              key={run.run_id}
              className="flex items-stretch mx-1 my-px rounded overflow-hidden"
              style={{
                backgroundColor: isSelected ? 'rgba(245,158,11,0.05)' : undefined,
              }}
            >
              {/* Checkbox column */}
              <button
                onClick={() => onToggleSelect(run.run_id)}
                title={isSelected ? 'Deselect' : 'Select for comparison'}
                className="flex items-center justify-center flex-shrink-0 px-2 rounded-l transition-colors"
                style={{
                  width: 28,
                  backgroundColor: 'transparent',
                  border: 'none',
                  cursor: 'pointer',
                }}
              >
                <span
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    width: 14,
                    height: 14,
                    borderRadius: 3,
                    border: `1.5px solid ${isSelected ? (selectionColor ?? '#f59e0b') : '#2d3140'}`,
                    backgroundColor: isSelected
                      ? `${selectionColor ?? '#f59e0b'}22`
                      : 'transparent',
                    flexShrink: 0,
                    fontSize: 9,
                    color: selectionColor ?? '#f59e0b',
                    transition: 'all 0.1s',
                  }}
                >
                  {isSelected && '✓'}
                </span>
              </button>

              {/* Nav link */}
              <NavLink
                to={`/runs/${run.run_id}`}
                className={({ isActive }) => `run-nav-link flex-1${isActive ? ' active' : ''}`}
                style={{ margin: 0, borderRadius: 0 }}
              >
                <div className="flex items-center gap-2 min-w-0">
                  <span
                    className={run.status === 'running' ? 'status-pulse' : ''}
                    style={{
                      display: 'inline-block',
                      width: 6,
                      height: 6,
                      borderRadius: '50%',
                      backgroundColor: STATUS_COLOR[run.status] ?? '#4a4e60',
                      flexShrink: 0,
                    }}
                  />
                  <span
                    className="truncate text-sm font-medium leading-snug"
                    style={{ color: '#e8eaf0' }}
                  >
                    {run.name}
                  </span>
                </div>
                <div className="flex items-center gap-2 mt-0.5 pl-[14px]">
                  <span className="text-xs font-mono" style={{ color: '#4a4e60' }}>
                    {timeAgo(run.created_at)}
                  </span>
                  {run.tags.slice(0, 2).map((tag) => (
                    <span key={tag} className="text-xs" style={{ color: '#4a4e60' }}>
                      #{tag}
                    </span>
                  ))}
                </div>
              </NavLink>
            </div>
          )
        })}
      </div>

      {/* Footer */}
      <div className="border-t" style={{ borderColor: '#252830' }}>
        {isComparing ? (
          <div className="flex">
            <button
              onClick={handleCompare}
              className="flex-1 px-4 py-2.5 text-xs font-mono text-left transition-colors"
              style={{
                backgroundColor: 'rgba(245, 158, 11, 0.1)',
                color: '#f59e0b',
                border: 'none',
                cursor: 'pointer',
              }}
            >
              Compare {selectedIds.length} runs →
            </button>
            <button
              onClick={onClearSelection}
              className="px-3 py-2.5 text-xs font-mono transition-colors"
              title="Clear selection"
              style={{
                backgroundColor: 'transparent',
                color: '#4a4e60',
                border: 'none',
                borderLeft: '1px solid #252830',
                cursor: 'pointer',
              }}
            >
              ✕
            </button>
          </div>
        ) : (
          <div className="px-4 py-2 text-xs font-mono" style={{ color: '#4a4e60' }}>
            {runs.length} run{runs.length !== 1 ? 's' : ''}
            {selectedIds.length === 1 && (
              <span style={{ color: '#8b8fa8' }}> · 1 selected</span>
            )}
          </div>
        )}
      </div>
    </aside>
  )
}

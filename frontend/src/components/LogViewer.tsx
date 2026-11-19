import { useMemo, useState } from 'react'
import type { RunLog } from '../api/types.ts'

interface Props {
  logs: RunLog[]
}

export default function LogViewer({ logs }: Props) {
  const [stream, setStream] = useState<'all' | 'stdout' | 'stderr'>('all')
  const [search, setSearch] = useState('')

  const filtered = useMemo(() => {
    let result = logs
    if (stream !== 'all') result = result.filter((l) => l.stream === stream)
    if (search) {
      const q = search.toLowerCase()
      result = result.filter((l) => l.line.toLowerCase().includes(q))
    }
    return result
  }, [logs, stream, search])

  if (logs.length === 0) return null

  const hasStderr = logs.some((l) => l.stream === 'stderr')

  return (
    <div className="rounded-lg" style={{ backgroundColor: '#0d0f14', border: '1px solid #252830' }}>
      {/* Controls bar */}
      <div
        className="flex items-center gap-3 px-3 py-2"
        style={{ borderBottom: '1px solid #1a1d24' }}
      >
        <div className="flex gap-1">
          {(['all', 'stdout', ...(hasStderr ? ['stderr'] : [])] as const).map((s) => (
            <button
              key={s}
              onClick={() => setStream(s as typeof stream)}
              className="text-xs font-mono px-2 py-0.5 rounded transition-colors"
              style={{
                backgroundColor: stream === s ? '#1e2128' : 'transparent',
                color:
                  stream === s
                    ? s === 'stderr'
                      ? '#f97316'
                      : '#8b8fa8'
                    : '#4a4e60',
                border: `1px solid ${stream === s ? '#353840' : 'transparent'}`,
                cursor: 'pointer',
              }}
            >
              {s}
            </button>
          ))}
        </div>

        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="filter…"
          style={{
            background: 'none',
            border: 'none',
            outline: 'none',
            color: '#8b8fa8',
            fontSize: 11,
            fontFamily: 'JetBrains Mono, monospace',
            flex: 1,
            minWidth: 0,
          }}
        />

        <span
          className="text-xs font-mono flex-shrink-0"
          style={{ color: '#4a4e60' }}
        >
          {filtered.length}/{logs.length}
        </span>
      </div>

      {/* Log lines */}
      <div
        style={{
          maxHeight: 320,
          overflowY: 'auto',
          padding: '8px 0',
          fontFamily: 'JetBrains Mono, monospace',
          fontSize: 11,
        }}
      >
        {filtered.length === 0 ? (
          <div
            className="flex items-center justify-center text-xs font-mono"
            style={{ height: 60, color: '#4a4e60' }}
          >
            no matching lines
          </div>
        ) : (
          filtered.map((entry, i) => (
            <div
              key={i}
              className="flex items-baseline gap-0 hover:bg-opacity-10"
              style={{ paddingLeft: 12, paddingRight: 12, lineHeight: 1.65 }}
            >
              {/* Step gutter */}
              <span
                style={{
                  color: '#2a2d38',
                  minWidth: 44,
                  textAlign: 'right',
                  flexShrink: 0,
                  paddingRight: 12,
                  fontSize: 10,
                  userSelect: 'none',
                }}
              >
                {entry.step != null ? entry.step : ''}
              </span>
              {/* Line content */}
              <span
                style={{
                  color: entry.stream === 'stderr' ? '#f97316' : '#8b8fa8',
                  wordBreak: 'break-all',
                  whiteSpace: 'pre-wrap',
                }}
              >
                {entry.line}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}

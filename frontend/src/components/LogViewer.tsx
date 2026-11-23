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
    <div className="rounded border border-py-border bg-py-n0">
      <div className="flex items-center gap-3 border-b border-py-border px-3 py-2">
        <div className="flex" role="group" aria-label="Log stream">
          {(['all', 'stdout', ...(hasStderr ? ['stderr'] : [])] as const).map((s) => (
            <button
              key={s}
              onClick={() => setStream(s as typeof stream)}
              aria-pressed={stream === s}
              className={`-ml-px h-7 border px-3 text-[13px] transition-colors first:ml-0 first:rounded-l last:rounded-r ${
                stream === s
                  ? 'border-py-border-hi bg-py-elevated text-py-text'
                  : 'border-py-border-hi bg-py-surface text-py-soft hover:bg-py-elevated hover:text-py-text'
              }`}
            >
              {s}
            </button>
          ))}
        </div>

        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter lines"
          aria-label="Filter log lines"
          className="h-7 min-w-0 flex-1 rounded border border-py-border-hi bg-py-elevated px-2 text-[13px] text-py-text placeholder:text-py-faint"
        />

        <span className="num flex-shrink-0 text-xs text-py-muted">
          {filtered.length}/{logs.length}
        </span>
      </div>

      <div className="max-h-80 overflow-y-auto py-2 font-mono text-xs">
        {filtered.length === 0 ? (
          <div className="flex h-[60px] items-center justify-center font-sans text-[13px] text-py-muted">
            No matching lines
          </div>
        ) : (
          filtered.map((entry, i) => (
            <div key={i} className="flex items-baseline px-3 leading-[1.65] hover:bg-py-surface">
              <span className="min-w-[44px] flex-shrink-0 select-none pr-3 text-right text-[11px] text-py-faint">
                {entry.step != null ? entry.step : ''}
              </span>
              <span
                className={`whitespace-pre-wrap break-all ${entry.stream === 'stderr' ? 'text-py-red/85' : 'text-py-soft'}`}
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

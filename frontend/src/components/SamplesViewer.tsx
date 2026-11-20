import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client.ts'
import type { SampleStep, SamplesPage } from '../api/types.ts'

export interface SamplesRun {
  runId: string
  label: string
  color?: string
}

interface PaneState {
  key: number
  runId: string
  label: string
  color?: string
  name: string
  step: number
}

const PAGE = 50
const CLIP = 240
const mono = 'JetBrains Mono, monospace'

/** Trim and collapse whitespace so pinned prompts match across formatting differences (server does the same). */
export function normWs(text: string): string {
  return text.split(/\s+/).filter(Boolean).join(' ')
}

function fmt(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toPrecision(4)
  return JSON.stringify(v)
}

/** One run + stream + step: server-paginated table with sort, filter and click-to-expand cells. */
function SamplePane({
  pane, steps, pin, onPin, onChange, onRemove,
}: {
  pane: PaneState
  steps: SampleStep[]
  pin: string | null
  onPin: (prompt: string | null) => void
  onChange: (p: Partial<PaneState>) => void
  onRemove?: () => void
}) {
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<SamplesPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const [q, setQ] = useState('')
  const [sortKey, setSortKey] = useState<string | null>(null)
  const [desc, setDesc] = useState(true)
  const [open, setOpen] = useState<Set<string>>(new Set())

  // Debounce the filter box; filtering/sorting/pinning run server-side over the whole step.
  useEffect(() => {
    const t = setTimeout(() => setQ(filter), 250)
    return () => clearTimeout(t)
  }, [filter])
  useEffect(() => { setOffset(0) }, [pane.runId, pane.name, pane.step, q, sortKey, desc, pin])
  useEffect(() => {
    let live = true
    setError(null)
    api.getSamples(pane.runId, pane.name, pane.step, offset, PAGE, { sort: sortKey, desc, q, prompt: pin })
      .then((p) => { if (live) setPage(p) })
      .catch((e) => { if (live) setError(String(e)) })
    return () => { live = false }
  }, [pane.runId, pane.name, pane.step, offset, q, sortKey, desc, pin])

  const items = page?.items ?? []
  // Columns accumulate across pages so headers (and sorting) survive an empty filter result.
  const [colState, setColState] = useState<{ id: string; cols: string[] }>({ id: '', cols: [] })
  const colId = `${pane.runId}|${pane.name}|${pane.step}`
  const columns = useMemo(() => {
    const keys = colState.id === colId ? [...colState.cols] : []
    for (const it of items) for (const k of Object.keys(it.sample)) if (!keys.includes(k)) keys.push(k)
    return keys
  }, [items, colId, colState])
  useEffect(() => {
    setColState((prev) => (prev.id === colId && prev.cols.join('\u0000') === columns.join('\u0000') ? prev : { id: colId, cols: columns }))
  }, [columns, colId])

  const rows = items

  const myStepsForName = steps.filter((s) => s.name === pane.name)
  const total = page?.total ?? 0
  const sel = { backgroundColor: '#0d0f14', border: '1px solid #252830', color: '#c8cad6', fontFamily: mono }

  return (
    <div className="rounded-lg min-w-0" style={{ backgroundColor: '#111318', border: '1px solid #252830' }}>
      <div className="flex flex-wrap items-center gap-2 px-3 py-2" style={{ borderBottom: '1px solid #1a1d24' }}>
        <span className="text-xs font-mono" style={{ color: pane.color ?? '#8b8fa8' }}>{pane.label}</span>
        <select className="text-xs rounded px-1 py-0.5" style={sel} value={pane.name}
          onChange={(e) => {
            const first = steps.find((s) => s.name === e.target.value)
            if (first) onChange({ name: first.name, step: first.step })
          }}>
          {[...new Set(steps.map((s) => s.name))].map((n) => <option key={n}>{n}</option>)}
        </select>
        <select className="text-xs rounded px-1 py-0.5" style={sel} value={pane.step}
          onChange={(e) => onChange({ step: Number(e.target.value) })}>
          {myStepsForName.map((s) => <option key={s.step} value={s.step}>step {s.step} ({s.count})</option>)}
        </select>
        <input className="text-xs rounded px-2 py-0.5 flex-1 min-w-[80px]" style={sel}
          placeholder="filter (whole step)" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {onRemove && (
          <button className="text-xs font-mono" style={{ color: '#6b6f85' }} onClick={onRemove}>remove</button>
        )}
      </div>
      {error && <div className="p-3 text-xs font-mono" style={{ color: '#ef4444' }}>{error}</div>}
      <div style={{ overflowX: 'auto', maxHeight: 520, overflowY: 'auto' }}>
        <table className="w-full text-xs" style={{ fontFamily: mono, borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ position: 'sticky', top: 0, backgroundColor: '#111318' }}>
              <th style={{ width: 28 }} />
              {columns.map((c) => (
                <th key={c} className="text-left px-2 py-1 cursor-pointer select-none"
                  style={{ color: '#8b8fa8', fontWeight: 500 }}
                  onClick={() => { if (sortKey === c) setDesc(!desc); else { setSortKey(c); setDesc(true) } }}>
                  {c}{sortKey === c ? (desc ? ' ▼' : ' ▲') : ''}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((it) => {
              const prompt = normWs(fmt(it.sample.prompt))
              return (
                <tr key={it.idx} style={{ borderTop: '1px solid #1a1d24', verticalAlign: 'top' }}>
                  <td className="px-1 py-1">
                    {'prompt' in it.sample && (
                      <button title={pin === prompt ? 'Unpin prompt' : 'Show this prompt in every pane'}
                        style={{ color: pin === prompt ? '#f59e0b' : '#4a4e60' }}
                        onClick={() => onPin(pin === prompt ? null : prompt)}>
                        ◆
                      </button>
                    )}
                  </td>
                  {columns.map((c) => {
                    const text = fmt(it.sample[c])
                    const id = `${it.idx}:${c}`
                    const long = text.length > CLIP
                    const expanded = open.has(id)
                    return (
                      <td key={c} className="px-2 py-1"
                        style={{ color: '#c8cad6', maxWidth: 420, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                        {long && !expanded ? text.slice(0, CLIP) + '…' : text}
                        {long && (
                          <button className="ml-1" style={{ color: '#6b8afd' }}
                            onClick={() => setOpen((prev) => {
                              const n = new Set(prev)
                              if (n.has(id)) n.delete(id); else n.add(id)
                              return n
                            })}>
                            {expanded ? 'less' : 'more'}
                          </button>
                        )}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
        {page && rows.length === 0 && (
          <div className="p-3 text-xs font-mono" style={{ color: '#4a4e60' }}>No matching samples.</div>
        )}
      </div>
      <div className="flex items-center gap-2 px-3 py-1.5 text-xs font-mono"
        style={{ borderTop: '1px solid #1a1d24', color: '#6b6f85' }}>
        <span>{total === 0 ? 0 : offset + 1}-{Math.min(offset + PAGE, total)} of {total}</span>
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>prev</button>
        <button disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>next</button>
      </div>
    </div>
  )
}

/**
 * Generation samples (run.log_samples). One pane per run in compare view; on a single run the
 * "add pane" button opens another step side by side. Pinning a prompt (◆) filters every pane to
 * rows with that exact prompt, so one prompt can be followed across steps and runs.
 */
export default function SamplesViewer({ runs }: { runs: SamplesRun[] }) {
  const [stepsByRun, setStepsByRun] = useState<Record<string, SampleStep[]>>({})
  const [panes, setPanes] = useState<PaneState[]>([])
  const [pin, setPin] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const runsKey = runs.map((r) => r.runId).join(',')

  useEffect(() => {
    let live = true
    setLoaded(false)
    Promise.all(runs.map((r) => api.getSampleSteps(r.runId).catch(() => [] as SampleStep[]))).then((all) => {
      if (!live) return
      const map: Record<string, SampleStep[]> = {}
      const init: PaneState[] = []
      runs.forEach((r, i) => {
        map[r.runId] = all[i]
        if (all[i].length > 0) {
          const last = all[i][all[i].length - 1]
          init.push({ key: init.length, runId: r.runId, label: r.label, color: r.color, name: last.name, step: last.step })
        }
      })
      setStepsByRun(map)
      setPanes(init)
      setLoaded(true)
    })
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runsKey])

  if (!loaded || panes.length === 0) return null
  const single = runs.length === 1

  return (
    <div className="mb-6">
      <h2 className="text-xs font-mono uppercase mb-3" style={{ color: '#c8cad6', letterSpacing: '0.12em' }}>
        Samples
      </h2>
      <div className="flex items-center gap-3 mb-2 text-xs font-mono" style={{ color: '#6b6f85' }}>
        {pin && <button onClick={() => setPin(null)} style={{ color: '#f59e0b' }}>clear pinned prompt</button>}
        {single && (
          <button style={{ color: '#6b8afd' }}
            onClick={() => setPanes((p) => [...p, { ...p[p.length - 1], key: Math.max(...p.map((x) => x.key)) + 1 }])}>
            + add pane to compare steps
          </button>
        )}
      </div>
      <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${Math.min(panes.length, 2)}, minmax(0, 1fr))` }}>
        {panes.map((p) => (
          <SamplePane key={p.key} pane={p} steps={stepsByRun[p.runId] ?? []} pin={pin} onPin={setPin}
            onChange={(c) => setPanes((all) => all.map((x) => (x.key === p.key ? { ...x, ...c } : x)))}
            onRemove={panes.length > 1 ? () => setPanes((all) => all.filter((x) => x.key !== p.key)) : undefined} />
        ))}
      </div>
    </div>
  )
}

import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client.ts'
import Icon from './Icon.tsx'
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
const selCls = 'h-7 rounded border border-py-border-hi bg-py-elevated px-2 text-[13px] text-py-text'
const btnCls = 'h-7 rounded border border-py-border-hi bg-py-surface px-3 text-[13px] text-py-soft transition-colors hover:bg-py-elevated hover:text-py-text disabled:cursor-not-allowed disabled:opacity-50'

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

  return (
    <div className="min-w-0 rounded border border-py-border bg-py-surface">
      <div className="flex flex-wrap items-center gap-2 border-b border-py-border px-3 py-2">
        <span className="font-mono text-xs" style={pane.color ? { color: pane.color } : undefined}>{pane.label}</span>
        <select className={selCls} value={pane.name} aria-label="Sample stream"
          onChange={(e) => {
            const first = steps.find((s) => s.name === e.target.value)
            if (first) onChange({ name: first.name, step: first.step })
          }}>
          {[...new Set(steps.map((s) => s.name))].map((n) => <option key={n}>{n}</option>)}
        </select>
        <select className={`${selCls} num`} value={pane.step} aria-label="Sample step"
          onChange={(e) => onChange({ step: Number(e.target.value) })}>
          {myStepsForName.map((s) => <option key={s.step} value={s.step}>step {s.step} ({s.count})</option>)}
        </select>
        <input className={`${selCls} min-w-[80px] flex-1 placeholder:text-py-faint`}
          placeholder="Filter the whole step" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {onRemove && <button className={btnCls} onClick={onRemove}>Remove pane</button>}
      </div>
      {error && <div className="p-3 font-mono text-xs text-py-red">{error}</div>}
      <div className="max-h-[520px] overflow-auto">
        <table className="w-full border-collapse text-[13px]">
          <thead>
            <tr className="sticky top-0 bg-py-surface">
              <th className="w-7" />
              {columns.map((c) => (
                <th key={c} className="cursor-pointer select-none px-2 py-1.5 text-left text-xs font-medium text-py-muted hover:text-py-text"
                  aria-sort={sortKey === c ? (desc ? 'descending' : 'ascending') : 'none'}
                  onClick={() => { if (sortKey === c) setDesc(!desc); else { setSortKey(c); setDesc(true) } }}>
                  <span className="inline-flex items-center gap-1">
                    {c}
                    {sortKey === c && <Icon name="chevron-down" size={12} className={desc ? '' : 'rotate-180'} />}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((it) => {
              const prompt = normWs(fmt(it.sample.prompt))
              return (
                <tr key={it.idx} className="border-t border-py-border align-top hover:bg-py-elevated">
                  <td className="px-1 py-1">
                    {'prompt' in it.sample && (
                      <button title={pin === prompt ? 'Unpin prompt' : 'Show this prompt in every pane'}
                        aria-label={pin === prompt ? 'Unpin prompt' : 'Pin prompt'}
                        aria-pressed={pin === prompt}
                        className={`transition-colors ${pin === prompt ? 'text-py-text' : 'text-py-faint hover:text-py-text'}`}
                        onClick={() => onPin(pin === prompt ? null : prompt)}>
                        <Icon name="pin" size={14} />
                      </button>
                    )}
                  </td>
                  {columns.map((c) => {
                    const raw = it.sample[c]
                    const text = fmt(raw)
                    const id = `${it.idx}:${c}`
                    const long = text.length > CLIP
                    const expanded = open.has(id)
                    const isNum = typeof raw === 'number'
                    return (
                      <td key={c}
                        className={`max-w-[420px] whitespace-pre-wrap break-words px-2 py-1 text-py-soft ${isNum ? 'num text-right text-xs text-py-text' : ''}`}>
                        {long && !expanded ? text.slice(0, CLIP) + '…' : text}
                        {long && (
                          <button className="ml-1 text-py-muted underline hover:text-py-text"
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
          <div className="p-3 text-[13px] text-py-muted">No matching samples.</div>
        )}
      </div>
      <div className="flex items-center gap-2 border-t border-py-border px-3 py-1.5 text-xs text-py-muted">
        <span className="num">{total === 0 ? 0 : offset + 1}-{Math.min(offset + PAGE, total)} of {total}</span>
        <button className={btnCls} disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Prev</button>
        <button className={btnCls} disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Next</button>
      </div>
    </div>
  )
}

/**
 * Generation samples (run.log_samples). One pane per run in compare view; on a single run the
 * "add pane" button opens another step side by side. Pinning a prompt filters every pane to
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
      <h2 className="mb-3 text-[15px] font-semibold text-py-text">Samples</h2>
      <div className="mb-2 flex items-center gap-3">
        {pin && <button className={btnCls} onClick={() => setPin(null)}>Clear pinned prompt</button>}
        {single && (
          <button className={btnCls}
            onClick={() => setPanes((p) => [...p, { ...p[p.length - 1], key: Math.max(...p.map((x) => x.key)) + 1 }])}>
            Add pane to compare steps
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

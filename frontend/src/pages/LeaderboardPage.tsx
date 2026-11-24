import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api/client.ts'
import StatusBadge from '../components/StatusBadge.tsx'
import type { Leaderboard, LeaderboardRow, Project, RunStatus } from '../api/types.ts'
import { formatNum } from '../utils/format.ts'

type Basis = 'last' | 'best'

const select = 'h-8 rounded border border-py-border-hi bg-py-elevated px-2 text-[13px] text-py-text'
const LOWER_IS_BETTER = /(loss|ppl|perplexity|error|err|wer|cer|mse|mae|latency|time)/i

/** The score of a run on a metric: its latest value, or its best ever (direction from the metric name). */
function scoreOf(row: LeaderboardRow, metric: string, basis: Basis, lower: boolean): number | null {
  const s = row.scores[metric]
  if (!s) return null
  return basis === 'last' ? s.last : lower ? s.min : s.max
}

/** Ranks eval metrics (`eval/*`) across runs, within a project and/or group. */
export default function LeaderboardPage() {
  const [params, setParams] = useSearchParams()
  const project = params.get('project') ?? ''
  const group = params.get('group') ?? ''
  const metricParam = params.get('metric')
  const basis: Basis = params.get('basis') === 'best' ? 'best' : 'last'
  const dirParam = params.get('dir')

  const [projects, setProjects] = useState<Project[]>([])
  const [data, setData] = useState<Leaderboard | null>(null)
  const [error, setError] = useState<string | null>(null)

  const set = (k: string, v: string) =>
    setParams((p) => {
      const n = new URLSearchParams(p)
      if (v) n.set(k, v)
      else n.delete(k)
      if (k === 'project') n.delete('group')
      return n
    }, { replace: true })

  useEffect(() => { api.listProjects().then(setProjects).catch(() => setProjects([])) }, [])
  useEffect(() => {
    let live = true
    setError(null)
    const load = () =>
      api.leaderboard({ project: project || undefined, group: group || undefined })
        .then((d) => live && setData(d))
        .catch((e) => live && setError(e instanceof Error ? e.message : 'Could not load the leaderboard'))
    load()
    const t = setInterval(load, 10_000)
    return () => { live = false; clearInterval(t) }
  }, [project, group])

  const groups = useMemo(() => [...new Set((data?.rows ?? []).map((r) => r.group).filter((g): g is string => !!g))].sort(), [data])
  const metrics = data?.metrics ?? []
  const metric = metricParam && metrics.includes(metricParam) ? metricParam : metrics[0] ?? ''
  const lower = dirParam ? dirParam === 'min' : LOWER_IS_BETTER.test(metric)

  const ranked = useMemo(() => {
    const rows = (data?.rows ?? []).map((r) => ({ r, v: scoreOf(r, metric, basis, lower) }))
    const have = rows.filter((x): x is { r: LeaderboardRow; v: number } => x.v != null)
    have.sort((a, b) => (lower ? a.v - b.v : b.v - a.v))
    return have
  }, [data, metric, basis, lower])
  const best = ranked[0]?.v
  const worst = ranked[ranked.length - 1]?.v

  return (
    <div className="mx-auto flex max-w-[1200px] flex-col gap-5 p-6">
      <div>
        <h1 className="text-[22px] font-semibold text-py-text">Leaderboard</h1>
        <p className="mt-1 text-[13px] text-py-muted">
          Runs ranked by an eval metric. Log scores with <code className="num">run.log({'{'}"eval/accuracy": 0.91{'}'})</code>; every <code className="num">eval/*</code> metric shows up here.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1"><span className="log-label">Project</span>
          <select className={select} value={project} onChange={(e) => set('project', e.target.value)}>
            <option value="">All projects</option>
            {projects.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1"><span className="log-label">Group</span>
          <select className={select} value={group} onChange={(e) => set('group', e.target.value)}>
            <option value="">All groups</option>
            {group && !groups.includes(group) && <option value={group}>{group}</option>}
            {groups.map((g) => <option key={g} value={g}>{g}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1"><span className="log-label">Rank by</span>
          <select className={select} value={metric} onChange={(e) => set('metric', e.target.value)} disabled={!metrics.length}>
            {metrics.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1"><span className="log-label">Score</span>
          <select className={select} value={basis} onChange={(e) => set('basis', e.target.value)}>
            <option value="last">Latest value</option>
            <option value="best">Best ever</option>
          </select>
        </label>
        <label className="flex flex-col gap-1"><span className="log-label">Better is</span>
          <select className={select} value={lower ? 'min' : 'max'} onChange={(e) => set('dir', e.target.value)}>
            <option value="max">Higher</option>
            <option value="min">Lower</option>
          </select>
        </label>
      </div>

      {error && <p className="text-[13px] text-py-red">{error}</p>}
      {!error && data && ranked.length === 0 && (
        <div className="rounded border border-py-border bg-py-surface p-5 text-[13px] text-py-soft">
          No runs have eval scores yet. Log them from your run, for example:
          <pre className="num mt-2 overflow-x-auto rounded border border-py-border bg-py-base p-3 text-xs text-py-text">{`run.log({"eval/accuracy": 0.91, "eval/f1": 0.88}, step=1000)`}</pre>
        </div>
      )}

      {ranked.length > 0 && (
        <div className="overflow-x-auto rounded border border-py-border bg-py-surface">
          <table className="w-full min-w-[640px] text-left text-[13px]">
            <thead>
              <tr className="border-b border-py-border text-py-muted">
                <th className="log-label w-10 px-3 py-2">#</th>
                <th className="log-label px-3 py-2">Run</th>
                <th className="log-label px-3 py-2">Group</th>
                {metrics.map((m) => (
                  <th key={m} className="px-3 py-2 text-right">
                    <button onClick={() => set('metric', m)} className={`num text-xs hover:text-py-text ${m === metric ? 'text-py-text underline underline-offset-4' : ''}`}>{m}</button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ranked.map(({ r, v }, i) => {
                const frac = best != null && worst != null && best !== worst ? Math.abs(v - worst) / Math.abs(best - worst) : 1
                return (
                  <tr key={r.run_id} className="border-b border-py-border last:border-0 hover:bg-py-elevated">
                    <td className="num px-3 py-2 text-py-muted">{i + 1}</td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-2">
                        <StatusBadge status={r.status as RunStatus} />
                        <Link to={`/runs/${r.run_id}`} className="text-py-text hover:underline">{r.name}</Link>
                      </div>
                      <div className="mt-1 h-[3px] w-40 bg-py-border"><div className="h-full bg-py-heat-4" style={{ width: `${Math.max(4, frac * 100)}%` }} /></div>
                    </td>
                    <td className="px-3 py-2 text-py-muted">{r.group ?? '–'}</td>
                    {metrics.map((m) => {
                      const s = scoreOf(r, m, basis, LOWER_IS_BETTER.test(m))
                      return <td key={m} className={`num px-3 py-2 text-right ${m === metric ? 'text-py-text' : 'text-py-soft'}`}>{s == null ? '–' : formatNum(s)}</td>
                    })}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

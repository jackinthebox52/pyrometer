import { describe, expect, it } from 'vitest'
import type { Run } from '../api/types.ts'
import {
  EMPTY_FILTERS,
  applyLiveRun,
  buildSidebarItems,
  mergeRuns,
  matchesFilters,
  parseTags,
  rangeBetween,
  sortRuns,
  filtersToQuery,
  statusSummary,
  summarizeStatuses,
  visibleRunIds,
} from './runList.ts'

function run(id: string, over: Partial<Run> = {}): Run {
  return {
    run_id: id,
    project: 'default',
    name: id,
    status: 'completed',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    finished_at: null,
    hyperparams: {},
    tags: [],
    notes: null,
    git_hash: null,
    git_dirty: false,
    gpu_model: null,
    gpu_memory_total: null,
    cuda_version: null,
    pytorch_version: null,
    python_version: null,
    hostname: null,
    ...over,
  }
}

describe('sortRuns', () => {
  const a = run('a', { name: 'zeta', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-03T00:00:00Z' })
  const b = run('b', { name: 'alpha', created_at: '2026-01-02T00:00:00Z', updated_at: '2026-01-02T00:00:00Z' })
  it('sorts by each key', () => {
    expect(sortRuns([a, b], 'updated').map((r) => r.run_id)).toEqual(['a', 'b'])
    expect(sortRuns([a, b], 'created').map((r) => r.run_id)).toEqual(['b', 'a'])
    expect(sortRuns([a, b], 'name').map((r) => r.run_id)).toEqual(['b', 'a'])
  })
  it('breaks ties by run_id and does not mutate', () => {
    const input = [run('y'), run('x')]
    expect(sortRuns(input, 'updated').map((r) => r.run_id)).toEqual(['x', 'y'])
    expect(input[0].run_id).toBe('y')
  })
})

describe('mergeRuns / applyLiveRun', () => {
  it('dedupes across pages with the incoming copy winning, and reorders', () => {
    const page1 = [run('a', { updated_at: '2026-01-03T00:00:00Z' }), run('b', { updated_at: '2026-01-02T00:00:00Z' })]
    const page2 = [run('b', { name: 'b2', updated_at: '2026-01-02T00:00:00Z' }), run('c', { updated_at: '2026-01-01T00:00:00Z' })]
    const merged = mergeRuns(page1, page2, 'updated')
    expect(merged.map((r) => r.run_id)).toEqual(['a', 'b', 'c'])
    expect(merged[1].name).toBe('b2')
  })
  it('a live update moves a run to the top', () => {
    const list = [run('a', { updated_at: '2026-01-03T00:00:00Z' }), run('b', { updated_at: '2026-01-02T00:00:00Z' })]
    const out = applyLiveRun(list, run('b', { updated_at: '2026-01-09T00:00:00Z' }), EMPTY_FILTERS, 'updated')
    expect(out.map((r) => r.run_id)).toEqual(['b', 'a'])
  })
  it('removes a run that was archived while archived are hidden, keeps it when shown', () => {
    const list = [run('a'), run('b')]
    const archived = run('a', { archived: true })
    expect(applyLiveRun(list, archived, EMPTY_FILTERS, 'updated').map((r) => r.run_id)).toEqual(['b'])
    expect(applyLiveRun(list, archived, { ...EMPTY_FILTERS, includeArchived: true }, 'updated')).toHaveLength(2)
  })
})

describe('matchesFilters', () => {
  it('applies status, project, group, tags (all), date range and query', () => {
    const r = run('a', { status: 'running', project: 'p', group: 'g', tags: ['x', 'y'], name: 'Baseline', created_at: '2026-03-10T12:00:00' })
    const f = { ...EMPTY_FILTERS }
    expect(matchesFilters(r, f)).toBe(true)
    expect(matchesFilters(r, { ...f, status: 'failed' })).toBe(false)
    expect(matchesFilters(r, { ...f, project: 'q' })).toBe(false)
    expect(matchesFilters(r, { ...f, group: 'h' })).toBe(false)
    expect(matchesFilters(r, { ...f, tags: ['x', 'y'] })).toBe(true)
    expect(matchesFilters(r, { ...f, tags: ['x', 'z'] })).toBe(false)
    expect(matchesFilters(r, { ...f, from: '2026-03-10', to: '2026-03-10' })).toBe(true)
    expect(matchesFilters(r, { ...f, from: '2026-03-11' })).toBe(false)
    expect(matchesFilters(r, { ...f, to: '2026-03-09' })).toBe(false)
    expect(matchesFilters(r, { ...f, query: 'BASE' })).toBe(true)
    expect(matchesFilters(r, { ...f, query: 'nope' })).toBe(false)
  })
  it('hides runs of archived projects unless archived are shown', () => {
    expect(matchesFilters(run('a', { project_archived: true }), EMPTY_FILTERS)).toBe(false)
  })
})

describe('buildSidebarItems', () => {
  const runs = [
    run('r1', { group: 'sweep', status: 'running' }),
    run('r2'),
    run('r3', { group: 'sweep', status: 'completed' }),
    run('r4', { group: 'sweep', project: 'other' }),
  ]
  it('nests runs under their group at the first member position, per project', () => {
    const items = buildSidebarItems(runs)
    expect(items.map((i) => i.kind)).toEqual(['group', 'run', 'group'])
    const g = items[0]
    if (g.kind !== 'group') throw new Error('expected group')
    expect(g.runs.map((r) => r.run_id)).toEqual(['r1', 'r3'])
    expect(g.summary).toBe('1 running · 1 completed')
  })
  it('lists visible ids honouring collapsed groups', () => {
    const items = buildSidebarItems(runs)
    const key = (items[0] as { key: string }).key
    expect(visibleRunIds(items, new Set())).toEqual(['r1', 'r3', 'r2', 'r4'])
    expect(visibleRunIds(items, new Set([key]))).toEqual(['r2', 'r4'])
  })
  it('summarises statuses', () => {
    expect(statusSummary([run('a', { status: 'failed' }), run('b', { status: 'running' }), run('c', { status: 'failed' })])).toBe('1 running · 2 failed')
  })
})

describe('helpers', () => {
  it('rangeBetween works in both directions', () => {
    expect(rangeBetween(['a', 'b', 'c', 'd'], 'c', 'a')).toEqual(['a', 'b', 'c'])
    expect(rangeBetween(['a', 'b'], 'a', 'zz')).toEqual([])
  })
  it('parseTags trims, strips #, dedupes', () => {
    expect(parseTags(' a, #b ,a,, ')).toEqual(['a', 'b'])
  })
})

describe('filtersToQuery', () => {
  it('maps every filter to a server parameter', () => {
    const q = filtersToQuery({ ...EMPTY_FILTERS, status: 'running', tags: ['a', 'b'], query: '  res ', project: 'p', group: 'g', includeArchived: true, from: '2024-03-01', to: '2024-03-01' })
    expect(q).toMatchObject({ status: 'running', tags: 'a,b', q: 'res', project: 'p', group: 'g', include_archived: true })
    expect(q.created_before! - q.created_after!).toBeGreaterThanOrEqual(82800) // one day (23-25h around DST)
    expect(q.created_before! - q.created_after!).toBeLessThanOrEqual(90000)
    expect(new Date(q.created_after! * 1000).getHours()).toBe(0)
  })
  it('leaves dates undefined when empty or invalid', () => {
    const q = filtersToQuery({ ...EMPTY_FILTERS, from: 'nope' })
    expect(q.created_after).toBeUndefined()
    expect(q.created_before).toBeUndefined()
  })
  it('summarizes server status counts', () => {
    expect(summarizeStatuses({ completed: 2, running: 1 })).toBe('1 running · 2 completed')
  })
})

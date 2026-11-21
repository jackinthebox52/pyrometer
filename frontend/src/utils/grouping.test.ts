import { describe, expect, it } from 'vitest'
import type { Run } from '../api/types.ts'
import { aggregate, groupRuns, parseIgnoreKeys } from './grouping.ts'
import { DEFAULT_COMPARE_STATE, sanitizeCompareState } from './compareState.ts'

const run = (id: string, hyperparams: Record<string, unknown>, tags: string[] = []): Run =>
  ({ run_id: id, name: id, hyperparams, tags }) as unknown as Run

describe('parseIgnoreKeys', () => {
  it('splits, trims and drops empties', () => {
    expect([...parseIgnoreKeys(' seed, run_idx ,,')]).toEqual(['seed', 'run_idx'])
    expect(parseIgnoreKeys('').size).toBe(0)
  })
})

describe('groupRuns', () => {
  const runs = [
    run('a1', { lr: 0.1, seed: 1 }),
    run('a2', { lr: 0.1, seed: 2 }),
    run('b1', { lr: 0.01, seed: 1 }),
  ]
  it('none: one group per run', () => {
    const g = groupRuns(runs, 'none', new Set())
    expect(g.map((x) => x.runIds)).toEqual([['a1'], ['a2'], ['b1']])
  })
  it('hyperparams: groups ignoring seed, labelled by differing keys', () => {
    const g = groupRuns(runs, 'hyperparams', new Set(['seed']))
    expect(g.map((x) => x.runIds)).toEqual([['a1', 'a2'], ['b1']])
    expect(g.map((x) => x.label)).toEqual(['lr=0.1', 'lr=0.01'])
  })
  it('hyperparams: without ignoring, seed splits groups', () => {
    expect(groupRuns(runs, 'hyperparams', new Set())).toHaveLength(3)
  })
  it('key order in nested hyperparams does not matter', () => {
    const g = groupRuns([run('x', { o: { a: 1, b: 2 } }), run('y', { o: { b: 2, a: 1 } })], 'hyperparams', new Set())
    expect(g).toHaveLength(1)
    expect(g[0].label).toBe('all runs')
  })
  it('missing hyperparams are tolerated', () => {
    const r = { run_id: 'z', name: 'z', tags: [] } as unknown as Run
    expect(groupRuns([r], 'hyperparams', new Set())).toHaveLength(1)
  })
  it('tag: ignores tags shared by every run; untagged label', () => {
    const g = groupRuns(
      [run('a', {}, ['all', 'x']), run('b', {}, ['all', 'x']), run('c', {}, ['all'])],
      'tag',
      new Set(),
    )
    expect(g.map((x) => x.runIds)).toEqual([['a', 'b'], ['c']])
    expect(g.map((x) => x.label)).toEqual(['x', 'untagged'])
  })
  it('handles an empty run list', () => {
    expect(groupRuns([], 'tag', new Set())).toEqual([])
  })
})

describe('aggregate', () => {
  const line = (ys: number[], xs = ys.map((_, i) => i)) => ys.map((y, i) => ({ x: xs[i], y }))
  it('needs at least two non-empty runs', () => {
    expect(aggregate([line([1, 2])], 'std')).toEqual([])
    expect(aggregate([line([1, 2]), []], 'std')).toEqual([])
    expect(aggregate([], 'minmax')).toEqual([])
  })
  it('computes mean and sample std', () => {
    const [p] = aggregate([line([1, -10, 10]), line([3, 10, -10])], 'std')
    expect(p).toMatchObject({ x: 0, mean: 2, n: 2 })
    expect(p.hi - p.mean).toBeCloseTo(Math.SQRT2)
    expect(p.mean - p.lo).toBeCloseTo(Math.SQRT2)
  })
  it('clamps a std band to the range the runs reached (no negative loss band)', () => {
    const [p] = aggregate([line([1]), line([3])], 'std')
    expect(p).toMatchObject({ mean: 2, lo: 1, hi: 3 })
    const out = aggregate([line([0.01, 0.01, 5]), line([0.02, 0.02, 0.01])], 'std')
    expect(out.every((q) => q.lo >= 0.01 && q.hi <= 5)).toBe(true)
  })
  it('min/max band', () => {
    const [p] = aggregate([line([1]), line([5]), line([3])], 'minmax')
    expect(p).toMatchObject({ mean: 3, lo: 1, hi: 5, n: 3 })
  })
  it('interpolates onto the union grid and never extrapolates', () => {
    const a = line([0, 10], [0, 10])
    const b = line([0, 4], [0, 4])
    const out = aggregate([a, b], 'minmax')
    // x=10 only covered by `a` -> dropped (needs >= 2 runs); x=4 interpolates a to 4
    expect(out.map((p) => p.x)).toEqual([0, 4])
    expect(out[1]).toMatchObject({ mean: 4, n: 2 })
  })
  it('interpolates between sparse points of one run', () => {
    const a = line([0, 10], [0, 10])
    const b = line([5, 5, 5], [0, 5, 10])
    const out = aggregate([a, b], 'minmax')
    expect(out.find((p) => p.x === 5)).toMatchObject({ lo: 5, hi: 5, mean: 5 })
  })
  it('thins very long grids', () => {
    const xs = Array.from({ length: 2000 }, (_, i) => i)
    const out = aggregate([line(xs.map(() => 1), xs), line(xs.map(() => 2), xs)], 'std')
    expect(out.length).toBeLessThanOrEqual(600)
    expect(out[0].x).toBe(0)
  })
  it('identical x in a single run does not divide by zero', () => {
    const out = aggregate([[{ x: 1, y: 1 }, { x: 1, y: 3 }], line([2, 2], [0, 2])], 'minmax')
    expect(out.every((p) => Number.isFinite(p.mean))).toBe(true)
  })
})

describe('compare state', () => {
  it('sanitizes stored values', () => {
    expect(sanitizeCompareState(null)).toEqual(DEFAULT_COMPARE_STATE)
    expect(sanitizeCompareState({ groupMode: 'tag', ignoreText: 'seed,x', band: 'minmax' })).toEqual({
      groupMode: 'tag',
      ignoreText: 'seed,x',
      band: 'minmax',
    })
    expect(sanitizeCompareState({ groupMode: 'bogus', ignoreText: 3, band: 'x' })).toEqual(DEFAULT_COMPARE_STATE)
  })
})

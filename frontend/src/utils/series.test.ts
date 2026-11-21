import { describe, expect, it } from 'vitest'
import { maxPointsForWidth, mergeBuckets, needsResample, tailStart, zoomQuery } from './series.ts'
import type { XPoint } from './xAxis.ts'

const pts = (xs: number[], steps = xs.map((_, i) => i * 10)): XPoint[] => xs.map((x, i) => ({ x, value: 0, step: steps[i] }))

describe('maxPointsForWidth', () => {
  it('is about 2 per pixel, clamped', () => {
    expect(maxPointsForWidth(500)).toBe(1000)
    expect(maxPointsForWidth(10)).toBe(200)
    expect(maxPointsForWidth(100000)).toBe(4000)
    expect(maxPointsForWidth(0)).toBe(1000)
  })
})

describe('zoomQuery', () => {
  it('step axis: rounded outward step window, order independent', () => {
    expect(zoomQuery({ axis: 'step', x0: 40.4, x1: 10.2 }, [])).toEqual({ step_min: 10, step_max: 41 })
  })
  it('time axis uses timestamps, rel adds the run start', () => {
    expect(zoomQuery({ axis: 'time', x0: 100, x1: 200 }, [])).toEqual({ t_min: 100, t_max: 200 })
    expect(zoomQuery({ axis: 'rel', x0: 5, x1: 15 }, [], 1000)).toEqual({ t_min: 1005, t_max: 1015 })
  })
  it('metric axis maps back to the steps of the points inside, widened by a neighbour', () => {
    const p = pts([0, 100, 200, 300, 400], [0, 10, 20, 30, 40])
    expect(zoomQuery({ axis: 'metric:tokens', x0: 150, x1: 350 }, p)).toEqual({ step_min: 10, step_max: 40 })
    expect(zoomQuery({ axis: 'metric:tokens', x0: 0, x1: 100 }, p)).toEqual({ step_min: 0, step_max: 20 })
  })
  it('metric axis with nothing inside, or a degenerate selection, is null', () => {
    expect(zoomQuery({ axis: 'metric:t', x0: 500, x1: 600 }, pts([0, 1]))).toBeNull()
    expect(zoomQuery({ axis: 'step', x0: 5, x1: 5 }, [])).toBeNull()
    expect(zoomQuery({ axis: 'step', x0: NaN, x1: 5 }, [])).toBeNull()
  })
  it('non-monotonic metric axis still yields a contiguous step window', () => {
    const p = pts([5, 1, 6], [0, 10, 20])
    const q = zoomQuery({ axis: 'metric:x', x0: 4, x1: 7 }, p)
    expect(q).toEqual({ step_min: 0, step_max: 20 })
  })
})

describe('mergeBuckets', () => {
  type R = { name: string; step: number; value: number }
  const key = (r: R) => `${r.name}\u0000${r.step}`
  const order = (a: R, b: R) => (a.name < b.name ? -1 : a.name > b.name ? 1 : a.step - b.step)
  it('appends new rows, replaces same-key rows, keeps order', () => {
    const a: R[] = [{ name: 'l', step: 0, value: 1 }, { name: 'l', step: 10, value: 2 }]
    const out = mergeBuckets(a, [{ name: 'l', step: 10, value: 9 }, { name: 'l', step: 20, value: 3 }, { name: 'a', step: 5, value: 0 }], key, order)
    expect(out.map((r) => [r.name, r.step, r.value])).toEqual([['a', 5, 0], ['l', 0, 1], ['l', 10, 9], ['l', 20, 3]])
  })
  it('returns the same array when nothing arrived', () => {
    const a: R[] = [{ name: 'l', step: 0, value: 1 }]
    expect(mergeBuckets(a, [], key, order)).toBe(a)
  })
})

describe('live helpers', () => {
  it('needsResample once rows exceed twice the budget', () => {
    expect(needsResample(1999, 1, 1000)).toBe(false)
    expect(needsResample(2001, 1, 1000)).toBe(true)
    expect(needsResample(5000, 3, 1000)).toBe(false)
  })
  it('tailStart backs off from the newest timestamp', () => {
    expect(tailStart([])).toBeUndefined()
    expect(tailStart([{ timestamp: 10 }, { timestamp: 100 }, { timestamp: 50 }])).toBe(95)
  })
})

import { describe, expect, it } from 'vitest'
import type { ParamStat } from '../api/types.ts'
import { binHeatmap } from './heatmap.ts'

const st = (param_name: string, step: number, grad_norm: number): ParamStat =>
  ({ step, timestamp: step, param_name, grad_norm, grad_min: 0, grad_max: 0, grad_mean: 0, weight_norm: 0, weight_min: 0, weight_max: 0, weight_mean: 0, grad_histogram: [], weight_histogram: [] })

describe('binHeatmap', () => {
  it('keeps one column per step when they fit', () => {
    const b = binHeatmap([st('a', 0, 1), st('a', 10, 2), st('b', 10, 3)], 10)
    expect(b.steps).toEqual([0, 10])
    expect(b.cells.get('a::1')).toBe(2)
    expect(b.cells.get('b::1')).toBe(3)
    expect(b.cells.has('b::0')).toBe(false)
  })
  it('aligns parameters that were downsampled to different steps, keeping the peak', () => {
    const rows: ParamStat[] = []
    for (let s = 0; s < 100; s++) rows.push(st('a', s, s === 37 ? 9 : 1))
    for (const s of [3, 60, 99]) rows.push(st('b', s, 5))
    const b = binHeatmap(rows, 10)
    expect(b.steps.length).toBe(10)
    expect(b.cells.get('a::3')).toBe(9) // step 37 falls in column 3 (steps 30-39)
    expect(b.cells.get('b::0')).toBe(5)
    expect(b.cells.get('b::6')).toBe(5)
    expect(b.ends[0]).toBe(9)
  })
  it('empty input', () => {
    expect(binHeatmap([], 5).steps).toEqual([])
  })
})

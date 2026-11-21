import { describe, expect, it } from 'vitest'
import type { ParamStat } from '../api/types.ts'
import { binHeatmap, columnAt, columnsToStepRange } from './heatmap.ts'

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

describe('drag selection helpers', () => {
  it('maps pixels to clamped columns', () => {
    expect(columnAt(0, 10, 4)).toBe(0)
    expect(columnAt(9, 10, 4)).toBe(2)
    expect(columnAt(-5, 10, 4)).toBe(0)
    expect(columnAt(500, 10, 4)).toBe(9)
    expect(columnAt(5, 0, 4)).toBeNull()
  })
  it('turns dragged columns (either direction) into a step window covering whole columns', () => {
    const bins = { steps: [0, 10, 20, 30], ends: [9, 19, 29, 39] }
    expect(columnsToStepRange(bins, 1, 2)).toEqual({ step_min: 10, step_max: 29 })
    expect(columnsToStepRange(bins, 3, 1)).toEqual({ step_min: 10, step_max: 39 })
    expect(columnsToStepRange(bins, 0, 9)).toBeNull()
  })
})

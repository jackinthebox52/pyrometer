import { describe, expect, it } from 'vitest'
import { CATEGORY_COLOR, LEGEND, colorDistance, opCategory, opColor } from './kernelColors.ts'

describe('kernel timeline colours', () => {
  it('has one unique colour per legend entry', () => {
    expect(new Set(LEGEND.map((l) => l.color)).size).toBe(LEGEND.length)
  })

  it('keeps every pair of categories clearly distinguishable', () => {
    const bad: string[] = []
    for (let i = 0; i < LEGEND.length; i++) {
      for (let j = i + 1; j < LEGEND.length; j++) {
        const d = colorDistance(LEGEND[i].color, LEGEND[j].color)
        if (d < 28) bad.push(`${LEGEND[i].label}/${LEGEND[j].label}=${d.toFixed(1)}`)
      }
    }
    expect(bad).toEqual([])
  })

  it('classifies common ops and only ever returns legend colours', () => {
    expect(opCategory('aten::addmm', 'cpu_op')).toBe('matmul')
    expect(opCategory('Optimizer.step#AdamW.step', 'cpu_op')).toBe('optimizer')
    expect(opCategory('aten::empty', 'memory_alloc')).toBe('memory')
    expect(opCategory('aten::embedding', 'cpu_op')).toBe('shape')
    expect(opCategory('something_unknown', 'cpu_op')).toBe('other')
    const colors = new Set(Object.values(CATEGORY_COLOR))
    for (const n of ['aten::conv2d', 'aten::relu', 'aten::index_select', 'weird']) expect(colors.has(opColor(n, 'cpu_op'))).toBe(true)
  })
})

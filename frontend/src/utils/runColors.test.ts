import { describe, expect, it } from 'vitest'
import { colorDistance } from './kernelColors.ts'
import { RUN_COLORS } from './runColors.ts'

describe('run colours', () => {
  it('has nine pairwise distinguishable colours', () => {
    expect(RUN_COLORS.length).toBeGreaterThanOrEqual(9)
    const bad: string[] = []
    for (let i = 0; i < RUN_COLORS.length; i++)
      for (let j = i + 1; j < RUN_COLORS.length; j++)
        if (colorDistance(RUN_COLORS[i], RUN_COLORS[j]) < 30) bad.push(`${i}/${j}`)
    expect(bad).toEqual([])
  })
})

import { describe, expect, it } from 'vitest'
import {
  BUILTIN_OPTIONS,
  applyPatch,
  clipForLog,
  ema,
  formatLogTick,
  logTicks,
  migrateLegacy,
  normalize,
  positiveRange,
  processSeries,
  resolveOptions,
  sanitizeOptions,
} from './chartOptions.ts'

describe('ema', () => {
  it('is the identity for alpha 0', () => {
    expect(ema([1, 2, 3], 0)).toEqual([1, 2, 3])
  })
  it('smooths and seeds with the first value', () => {
    expect(ema([10, 0], 0.5)).toEqual([10, 5])
  })
  it('skips null and non-finite entries without resetting', () => {
    expect(ema([10, null, NaN, 0], 0.5)).toEqual([10, null, null, 5])
  })
})

describe('normalize', () => {
  it('none returns the input', () => {
    const v = [1, 2]
    expect(normalize(v, 'none', 'loss')).toBe(v)
  })
  it('first divides by the first finite value', () => {
    expect(normalize([null, 2, 4], 'first', 'acc')).toEqual([null, 1, 2])
  })
  it('best uses min for lower-is-better metrics and max otherwise, as a percentage', () => {
    expect(normalize([2, 4], 'best', 'train/loss')).toEqual([100, 200])
    expect(normalize([2, 4], 'best', 'accuracy')).toEqual([50, 100])
  })
  it('zero reference yields all null; empty/no finite values pass through', () => {
    expect(normalize([0, 1], 'first', 'x')).toEqual([null, null])
    expect(normalize([NaN], 'first', 'x')).toEqual([NaN])
  })
  it('maps non-finite entries to null', () => {
    expect(normalize([1, Infinity, 2], 'first', 'x')).toEqual([1, null, 2])
  })
})

describe('clipForLog', () => {
  it('nulls values <= 0 and counts them', () => {
    expect(clipForLog([1, 0, -2, null, 3])).toEqual({ values: [1, null, null, null, 3], dropped: 2 })
  })
})

describe('processSeries', () => {
  it('applies normalize, then EMA, then log clipping', () => {
    const o = { ...BUILTIN_OPTIONS, logY: true, alpha: 0.5, norm: 'first' as const }
    const r = processSeries([2, 0, 2], o, 'x')
    expect(r.raw).toEqual([1, null, 1])
    // the zero is hidden in the raw curve, but the EMA (0.5*1 + 0.5*0 = 0.5) is positive and stays visible
    expect(r.smooth).toEqual([1, 0.5, 0.75])
    expect(r.dropped).toBe(1)
  })
  it('does not clip without logY', () => {
    expect(processSeries([0, 1], BUILTIN_OPTIONS, 'x')).toEqual({ raw: [0, 1], smooth: [0, 1], dropped: 0 })
  })
})

describe('option resolution and storage helpers', () => {
  it('resolves built-in < defaults < overrides', () => {
    expect(resolveOptions({ logY: true, alpha: 0.3 }, { alpha: 0.9 })).toEqual({
      ...BUILTIN_OPTIONS,
      logY: true,
      alpha: 0.9,
    })
  })
  it('applyPatch drops overrides equal to the default and clamps alpha', () => {
    const defaults = resolveOptions({ logY: true }, {})
    expect(applyPatch({ logY: false }, { logY: true }, defaults)).toEqual({})
    expect(applyPatch({}, { logY: false }, defaults)).toEqual({ logY: false })
    expect(applyPatch({}, { alpha: 5 }, defaults)).toEqual({ alpha: 0.99 })
  })
  it('sanitizeOptions rejects malformed fields', () => {
    expect(sanitizeOptions({ logY: 'yes', alpha: NaN, norm: 'weird', xAxis: '' })).toEqual({})
    expect(sanitizeOptions({ alpha: -1, norm: 'best', xAxis: 'rel' })).toEqual({ alpha: 0, norm: 'best', xAxis: 'rel' })
    expect(sanitizeOptions(null)).toEqual({})
  })
  it('migrates legacy full records to overrides, dropping built-in values', () => {
    expect(migrateLegacy({ logY: true, alpha: 0, norm: 'none', xAxis: 'step' })).toEqual({ logY: true })
    expect(migrateLegacy({ logY: false, alpha: 0.4, norm: 'first', xAxis: 'time' })).toEqual({
      alpha: 0.4,
      norm: 'first',
      xAxis: 'time',
    })
    expect(migrateLegacy('garbage')).toEqual({})
  })
})

describe('logTicks', () => {
  it('returns null without positive data', () => {
    expect(logTicks(0, 5)).toBeNull()
    expect(logTicks(-1, 5)).toBeNull()
    expect(logTicks(NaN, 5)).toBeNull()
    expect(logTicks(5, 1)).toBeNull()
  })
  it('snaps wide ranges to decades with decade ticks', () => {
    const t = logTicks(0.0004, 3)!
    expect(t.domain).toEqual([1e-4, 10])
    expect(t.ticks).toEqual([1e-4, 1e-3, 1e-2, 1e-1, 1, 10])
    expect(t.minor.length).toBe(8 * 5)
  })
  it('handles exact powers of ten without an extra decade', () => {
    const t = logTicks(1e-3, 1)!
    expect(t.domain).toEqual([1e-3, 1])
    expect(t.ticks).toEqual([1e-3, 1e-2, 1e-1, 1])
  })
  it('thins ticks and drops minor lines for very wide ranges', () => {
    const t = logTicks(1e-12, 1e3)!
    expect(t.ticks.length).toBeLessThanOrEqual(9)
    expect(t.minor).toEqual([])
  })
  it('uses 1/2/5 ticks inside short ranges and keeps the data range', () => {
    const t = logTicks(0.15, 0.9)!
    expect(t.domain).toEqual([0.15, 0.9])
    expect(t.ticks).toEqual([0.2, 0.5])
  })
  it('never returns fewer than a labelled tick for degenerate ranges', () => {
    expect(logTicks(3, 3)!.ticks).toEqual([3])
    expect(logTicks(1.1, 1.2)!.ticks).toEqual([1.1, 1.2])
  })
})

describe('formatLogTick', () => {
  it('uses plain numbers in the readable range and scientific outside', () => {
    expect(formatLogTick(0.01)).toBe('0.01')
    expect(formatLogTick(1)).toBe('1')
    expect(formatLogTick(1000)).toBe('1000')
    expect(formatLogTick(0.001)).toBe('1e-3')
    expect(formatLogTick(2e-5)).toBe('2e-5')
    expect(formatLogTick(1e6)).toBe('1e6')
    expect(formatLogTick(0)).toBe('')
  })
})

describe('positiveRange', () => {
  it('scans values and band arrays, skipping x and __n keys and non-positive values', () => {
    const rows = [
      { x: 0.0001, a: 5, 'a__band': [0.5, 9], 'a__n': 1000 },
      { x: 2, a: null, b: -3, c: 0 },
    ]
    expect(positiveRange(rows)).toEqual([0.5, 9])
  })
  it('returns null when nothing is positive', () => {
    expect(positiveRange([{ x: 1, a: 0, b: null }])).toBeNull()
  })
})

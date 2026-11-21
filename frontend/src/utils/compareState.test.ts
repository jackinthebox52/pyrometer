import { describe, expect, it } from 'vitest'
import { BUILTIN_OPTIONS } from './chartOptions.ts'
import {
  DEFAULT_COMPARE_STATE,
  decodeChartDefaults,
  decodeCompareState,
  decodeIds,
  sameChartDefaults,
  sanitizeCompareState,
  withChartDefaults,
  withCompareState,
} from './compareState.ts'

const P = (s: string) => new URLSearchParams(s)

describe('compare state in the URL', () => {
  it('round-trips every grouping combination without touching ids', () => {
    for (const groupMode of ['none', 'hyperparams', 'tag'] as const) {
      for (const band of ['std', 'minmax'] as const) {
        for (const ignoreText of ['seed', '', 'seed, run_idx', 'a&b=c,é']) {
          const s = { groupMode, band, ignoreText }
          const url = withCompareState(P('ids=a,b'), s)
          expect(url.get('ids')).toBe('a,b')
          expect(decodeCompareState(P(url.toString()))).toEqual(s)
        }
      }
    }
  })
  it('falls back to the last-used state only when the URL carries no grouping fields', () => {
    const last = { groupMode: 'tag', ignoreText: 'x', band: 'minmax' } as const
    expect(decodeCompareState(P('ids=a,b'), last)).toEqual(last)
    expect(decodeCompareState(P('ids=a,b&band=minmax'), last)).toEqual({ ...DEFAULT_COMPARE_STATE, band: 'minmax' })
  })
  it('ignores invalid values and always writes group', () => {
    expect(decodeCompareState(P('group=bogus&band=zzz'))).toEqual(DEFAULT_COMPARE_STATE)
    expect(withCompareState(P(''), DEFAULT_COMPARE_STATE).toString()).toBe('group=none')
  })
  it('decodes ids: trims, drops empties and duplicates', () => {
    expect(decodeIds(P('ids=a,,b, a ,c'))).toEqual(['a', 'b', 'c'])
    expect(decodeIds(P(''))).toEqual([])
  })
  it('sanitizes stored state', () => {
    expect(sanitizeCompareState({ groupMode: 'x', band: 'minmax' })).toEqual({ ...DEFAULT_COMPARE_STATE, band: 'minmax' })
    expect(sanitizeCompareState(null)).toEqual(DEFAULT_COMPARE_STATE)
  })
})

describe('chart defaults in the URL', () => {
  it('round-trips', () => {
    const c = { xAxis: 'metric:tokens seen', logY: true, norm: 'best', alpha: 0.9 } as const
    const url = withChartDefaults(P('ids=a,b'), c)
    const back = decodeChartDefaults(P(url.toString()))
    expect(back).not.toBeNull()
    expect(sameChartDefaults(back!, c)).toBe(true)
    expect(back!.xAxis).toBe('metric:tokens seen')
  })
  it('built-in defaults encode to just x, absent params mean built-in once any is present', () => {
    expect(withChartDefaults(P(''), BUILTIN_OPTIONS).toString()).toBe('x=step')
    expect(decodeChartDefaults(P('x=time'))).toEqual({ ...BUILTIN_OPTIONS, xAxis: 'time' })
    expect(decodeChartDefaults(P('ids=a'))).toBeNull()
  })
  it('clamps smoothing and rejects junk', () => {
    expect(decodeChartDefaults(P('smooth=5'))!.alpha).toBe(0.99)
    expect(decodeChartDefaults(P('x=step&norm=evil&smooth=abc'))).toEqual(BUILTIN_OPTIONS)
  })
})

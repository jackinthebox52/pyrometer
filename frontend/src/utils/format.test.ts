import { describe, expect, it } from 'vitest'
import { formatNum } from './format.ts'

describe('formatNum', () => {
  it('keeps at most 4 significant digits and trims zeros', () => {
    expect(formatNum(12)).toBe('12')
    expect(formatNum(9.0)).toBe('9')
    expect(formatNum(2.17)).toBe('2.17')
    expect(formatNum(0.89291)).toBe('0.8929')
    expect(formatNum(93.809)).toBe('93.81')
    expect(formatNum(-0.5)).toBe('-0.5')
    expect(formatNum(0.0003)).toBe('3e-4')
    expect(formatNum(0.0012)).toBe('0.0012')
  })
  it('uses suffixes from 1e4', () => {
    expect(formatNum(48700)).toBe('48.7k')
    expect(formatNum(4.32e10)).toBe('43.2B')
    expect(formatNum(1.05e6)).toBe('1.05M')
    expect(formatNum(6e10)).toBe('60B')
    expect(formatNum(2e12)).toBe('2T')
    expect(formatNum(999600)).toBe('1M')
    expect(formatNum(-48700)).toBe('-48.7k')
    expect(formatNum(9999.6)).toBe('10k')
  })
  it('uses compact scientific for small values', () => {
    expect(formatNum(7.1e-5)).toBe('7.1e-5')
    expect(formatNum(3e-4)).toBe('3e-4')
    expect(formatNum(-2.5e-7)).toBe('-2.5e-7')
  })
  it('handles zero and non-finite', () => {
    expect(formatNum(0)).toBe('0')
    expect(formatNum(NaN)).toBe('—')
    expect(formatNum(Infinity)).toBe('—')
  })
})

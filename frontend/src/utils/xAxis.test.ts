import { describe, expect, it } from 'vitest'
import type { Metric } from '../api/types.ts'
import {
  axisLabel,
  axisMetricName,
  eventX,
  firstTimestamp,
  formatXLabel,
  formatXTick,
  resolveXs,
  runHasAxis,
} from './xAxis.ts'

const m = (step: number, value: number, timestamp = 1000 + step, name = 'loss'): Metric => ({ step, value, timestamp, name })

describe('axis helpers', () => {
  it('parses metric axes and labels', () => {
    expect(axisMetricName('metric:tokens')).toBe('tokens')
    expect(axisMetricName('step')).toBeNull()
    expect(axisLabel('time')).toBe('time')
    expect(axisLabel('rel')).toBe('elapsed')
    expect(axisLabel('metric:tokens')).toBe('tokens')
    expect(axisLabel('step')).toBe('step')
  })
  it('runHasAxis requires the metric to exist', () => {
    expect(runHasAxis({}, 'step')).toBe(true)
    expect(runHasAxis({}, 'metric:t')).toBe(false)
    expect(runHasAxis({ byName: { t: [m(0, 1)] } }, 'metric:t')).toBe(true)
    expect(runHasAxis({ byName: { t: [] } }, 'metric:t')).toBe(false)
  })
})

describe('resolveXs', () => {
  const data = [m(2, 20), m(0, 0), m(1, 10)]
  it('step axis sorts by step', () => {
    const r = resolveXs(data, 'step', {})
    expect(r.points.map((p) => p.x)).toEqual([0, 1, 2])
    expect(r.nonMonotonic).toBe(false)
  })
  it('time axis uses timestamps; rel subtracts the run start', () => {
    expect(resolveXs(data, 'time', {}).points.map((p) => p.x)).toEqual([1000, 1001, 1002])
    expect(resolveXs(data, 'rel', { startTime: 990 }).points.map((p) => p.x)).toEqual([10, 11, 12])
  })
  it('rel falls back to the lowest-step timestamp without startTime', () => {
    expect(resolveXs(data, 'rel', {}).points.map((p) => p.x)).toEqual([0, 1, 2])
  })
  it('metric axis joins on step and counts points without an axis value', () => {
    const tokens = [m(0, 100, 0, 't'), m(2, 300, 0, 't'), m(3, 400, 0, 't')]
    const r = resolveXs(data, 'metric:t', { byName: { t: tokens } })
    expect(r.points.map((p) => [p.x, p.value])).toEqual([
      [100, 0],
      [300, 20],
    ])
    expect(r.skipped).toBe(1)
  })
  it('skips non-finite axis values', () => {
    const r = resolveXs([m(0, 1), m(1, 2)], 'metric:t', { byName: { t: [m(0, NaN, 0, 't'), m(1, 5, 0, 't')] } })
    expect(r.points.map((p) => p.x)).toEqual([5])
    expect(r.skipped).toBe(1)
  })
  it('flags a non-monotonic axis and sorts points by x', () => {
    const t = [m(0, 50, 0, 't'), m(1, 10, 0, 't'), m(2, 30, 0, 't')]
    const r = resolveXs([m(0, 1), m(1, 2), m(2, 3)], 'metric:t', { byName: { t } })
    expect(r.nonMonotonic).toBe(true)
    expect(r.points.map((p) => p.x)).toEqual([10, 30, 50])
    expect(r.points.map((p) => p.value)).toEqual([2, 3, 1])
  })
  it('equal x values are monotonic and keep step order', () => {
    const t = [m(0, 5, 0, 't'), m(1, 5, 0, 't')]
    const r = resolveXs([m(0, 1), m(1, 2)], 'metric:t', { byName: { t } })
    expect(r.nonMonotonic).toBe(false)
    expect(r.points.map((p) => p.value)).toEqual([1, 2])
  })
  it('handles empty data', () => {
    expect(resolveXs([], 'rel', {})).toEqual({ points: [], skipped: 0, nonMonotonic: false })
  })
})

describe('eventX', () => {
  const byName = { t: [m(0, 100, 0, 't'), m(10, 500, 0, 't')] }
  it('step axis needs a step', () => {
    expect(eventX({ step: 7, timestamp: 1 }, 'step', {})).toBe(7)
    expect(eventX({ step: null, timestamp: 1 }, 'step', {})).toBeNull()
  })
  it('time axes place step-less events by timestamp', () => {
    expect(eventX({ step: null, timestamp: 1234 }, 'time', {})).toBe(1234)
    expect(eventX({ step: null, timestamp: 1234 }, 'rel', { startTime: 1000 })).toBe(234)
  })
  it("'rel' without startTime falls back like resolveXs (first point timestamp)", () => {
    const data = [m(5, 1, 2000), m(1, 1, 1500)]
    const start = firstTimestamp(data)
    expect(start).toBe(1500)
    expect(eventX({ step: null, timestamp: 1600 }, 'rel', {}, start)).toBe(100)
    // consistent with resolveXs for the same point
    expect(resolveXs(data, 'rel', {}).points[0].x).toBe(eventX({ step: 1, timestamp: 1500 }, 'rel', {}, start))
    expect(eventX({ step: null, timestamp: 1600 }, 'rel', {})).toBeNull()
  })
  it('metric axes use the latest axis value at or before the event step', () => {
    expect(eventX({ step: 5, timestamp: 0 }, 'metric:t', { byName })).toBe(100)
    expect(eventX({ step: 10, timestamp: 0 }, 'metric:t', { byName })).toBe(500)
    expect(eventX({ step: 99, timestamp: 0 }, 'metric:t', { byName })).toBe(500)
    expect(eventX({ step: null, timestamp: 0 }, 'metric:t', { byName })).toBeNull()
    expect(eventX({ step: 5, timestamp: 0 }, 'metric:missing', { byName })).toBeNull()
    const early = { byName: { t: [m(3, 9, 0, 't')] } }
    expect(eventX({ step: 1, timestamp: 0 }, 'metric:t', early)).toBeNull()
  })
})

describe('firstTimestamp', () => {
  it('returns the timestamp of the lowest step, undefined when empty', () => {
    expect(firstTimestamp([m(3, 0, 30), m(1, 0, 10)])).toBe(10)
    expect(firstTimestamp([])).toBeUndefined()
  })
})

describe('formatting', () => {
  it('formats elapsed ticks', () => {
    const f = formatXTick('rel')
    expect(f(30)).toBe('30s')
    expect(f(90)).toBe('1m 30s')
    expect(f(3600)).toBe('1h')
    expect(f(5400)).toBe('1h 30m')
  })
  it('formats compact numeric ticks and labels', () => {
    const f = formatXTick('step')
    expect(f(5)).toBe('5')
    expect(f(25000)).toBe('25k')
    expect(f(2.5e6)).toBe('2.5M')
    expect(formatXLabel('step', 10)).toBe('step 10')
    expect(formatXLabel('rel', 61)).toBe('elapsed 1m 1s')
  })
})

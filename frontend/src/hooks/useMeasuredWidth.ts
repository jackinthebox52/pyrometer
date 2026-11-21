import { useEffect, useRef } from 'react'
import type { RefObject } from 'react'
import { maxPointsForWidth } from '../utils/series.ts'

/**
 * Live widths of the mounted charts (measured with a ResizeObserver, not derived from the page width).
 * Series budgets (`max_points`) follow the chart that will actually draw them: a zoom refetch uses its own
 * chart's width; page-wide loads use the widest chart currently on screen.
 */
const widths = new Map<symbol, number>()
const listeners = new Set<() => void>()

function notify(): void {
  listeners.forEach((fn) => fn())
}

/** Width of the widest mounted chart in px, or `undefined` if none has been measured yet. */
export function widestChartWidth(): number | undefined {
  let best: number | undefined
  for (const w of widths.values()) if (w > 0 && (best === undefined || w > best)) best = w
  return best
}

/** Points budget for the widest chart, falling back to a page-derived estimate before any chart is measured. */
export function chartPointsBudget(fallbackWidth: number): number {
  return maxPointsForWidth(widestChartWidth() ?? fallbackWidth)
}

/** Call `fn` whenever any chart's measured width changes. Returns the unsubscribe function. */
export function onChartWidthChange(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/**
 * Track the width of `ref`'s element with a ResizeObserver. Returns a ref holding the current width in px
 * (read it in fetch callbacks: it never goes stale and a resize does not re-render) and registers the
 * element in the shared registry. Without ResizeObserver (old browsers, tests) the width is read once.
 */
export function useMeasuredWidth(ref: RefObject<HTMLElement | null>, fallback = 600): { current: number } {
  const width = useRef(fallback)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const id = Symbol('chart')
    const set = (w: number) => {
      if (w <= 0 || Math.round(w) === width.current) return
      width.current = Math.round(w)
      widths.set(id, width.current)
      notify()
    }
    set(el.clientWidth || el.getBoundingClientRect().width)
    widths.set(id, width.current)
    if (typeof ResizeObserver === 'undefined') {
      return () => {
        widths.delete(id)
      }
    }
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) set(e.contentRect.width)
    })
    ro.observe(el)
    return () => {
      ro.disconnect()
      widths.delete(id)
      notify()
    }
  }, [ref])
  return width
}

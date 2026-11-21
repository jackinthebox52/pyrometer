import { useCallback, useRef, useState } from 'react'

interface ChartMouse {
  activeLabel?: string | number
}

/**
 * Drag-to-select on a Recharts chart with a numeric x axis. Spread `handlers` on the chart and draw a
 * ReferenceArea from `area` while selecting. `onSelect(x0, x1)` fires on mouse-up with a non-empty selection
 * (x0 < x1); a plain click or leaving the chart cancels. Pass `undefined` to disable.
 */
export function useDragZoom(onSelect: ((x0: number, x1: number) => void) | undefined) {
  const [area, setArea] = useState<[number, number] | null>(null)
  const anchor = useRef<number | null>(null)
  const cursor = useRef<number | null>(null)

  const cancel = useCallback(() => {
    anchor.current = null
    cursor.current = null
    setArea(null)
  }, [])

  const down = useCallback((e: ChartMouse | null) => {
    const x = e?.activeLabel == null ? NaN : Number(e.activeLabel)
    if (!Number.isFinite(x)) return
    anchor.current = x
    cursor.current = x
  }, [])

  const move = useCallback((e: ChartMouse | null) => {
    if (anchor.current == null) return
    const x = e?.activeLabel == null ? NaN : Number(e.activeLabel)
    if (!Number.isFinite(x)) return
    cursor.current = x
    setArea([anchor.current, x])
  }, [])

  const up = useCallback(() => {
    const a = anchor.current
    const b = cursor.current
    cancel()
    if (a != null && b != null && a !== b) onSelect?.(Math.min(a, b), Math.max(a, b))
  }, [cancel, onSelect])

  if (!onSelect) return { area: null, handlers: {} as Record<string, never> }
  return { area, handlers: { onMouseDown: down, onMouseMove: move, onMouseUp: up, onMouseLeave: cancel } }
}

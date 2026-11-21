import { useCallback, useEffect, useRef, useState } from 'react'
import { sharedZoomFor } from '../utils/series.ts'
import type { XZoom } from '../utils/series.ts'

/**
 * Self-managed zoom: remember the selection and fetch what it covers at full resolution (the caller's
 * `fetchWindow` turns the selection into a server query). `refreshKey` re-fetches the window (live runs).
 * `fetchWindow` returning null means the selection was empty: the zoom is dropped.
 *
 * With `linked` the selection lives outside (shared by several charts, each fetching its own window); a chart
 * whose `axis` differs from the shared selection's ignores it instead of resetting it.
 */
export interface LinkedZoom {
  zoom: XZoom | null
  set: (zoom: XZoom | null) => void
}

export function useZoomFetch<T>(
  fetchWindow: ((zoom: XZoom) => Promise<T | null>) | undefined,
  refreshKey: unknown,
  linked?: LinkedZoom,
  axis?: string,
) {
  const [ownZoom, setOwnZoom] = useState<XZoom | null>(null)
  const zoom = linked ? sharedZoomFor(linked.zoom, axis) : ownZoom
  const setZoom = linked ? linked.set : setOwnZoom
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState(false)
  const fetchRef = useRef(fetchWindow)
  fetchRef.current = fetchWindow
  const gen = useRef(0)

  useEffect(() => {
    const f = fetchRef.current
    if (!zoom || !f) return
    const g = ++gen.current
    setLoading(true)
    f(zoom)
      .then((d) => {
        if (g !== gen.current) return
        if (d == null) setZoom(null) // empty selection (setZoom is stable per hook mode)
        else {
          setData(d)
          setFailed(false)
        }
      })
      .catch(() => g === gen.current && setFailed(true))
      .finally(() => g === gen.current && setLoading(false))
  }, [zoom, refreshKey])

  const setRef = useRef(setZoom)
  setRef.current = setZoom
  const reset = useCallback(() => {
    gen.current++
    setRef.current(null)
    setData(null)
    setLoading(false)
    setFailed(false)
  }, [])
  const select = useCallback((z: XZoom | null) => setRef.current(z), [])

  return { zoom, data: zoom ? data : null, loading, failed, select, reset }
}

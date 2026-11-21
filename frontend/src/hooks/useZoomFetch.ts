import { useCallback, useEffect, useRef, useState } from 'react'
import type { XZoom } from '../utils/series.ts'

/**
 * Self-managed zoom: remember the selection and fetch what it covers at full resolution (the caller's
 * `fetchWindow` turns the selection into a server query). `refreshKey` re-fetches the window (live runs).
 * `fetchWindow` returning null means the selection was empty: the zoom is dropped.
 */
export function useZoomFetch<T>(fetchWindow: ((zoom: XZoom) => Promise<T | null>) | undefined, refreshKey: unknown) {
  const [zoom, setZoom] = useState<XZoom | null>(null)
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
        if (d == null) setZoom(null)
        else {
          setData(d)
          setFailed(false)
        }
      })
      .catch(() => g === gen.current && setFailed(true))
      .finally(() => g === gen.current && setLoading(false))
  }, [zoom, refreshKey])

  const reset = useCallback(() => {
    gen.current++
    setZoom(null)
    setData(null)
    setLoading(false)
    setFailed(false)
  }, [])

  return { zoom, data: zoom ? data : null, loading, failed, select: setZoom, reset }
}

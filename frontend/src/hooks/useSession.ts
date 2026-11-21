import { useEffect, useState } from 'react'
import { api } from '../api/client.ts'
import type { Session } from '../api/types.ts'

let _cached: Promise<Session> | null = null

/**
 * The current session (cached for the page's lifetime). `isAdmin` gates the organising writes that
 * dashboard sessions are allowed (docs/auth.md): only admins may rename, tag, archive, move or delete.
 */
export function useSession(): { session: Session | null; isAdmin: boolean } {
  const [session, setSession] = useState<Session | null>(null)
  useEffect(() => {
    let live = true
    if (!_cached) _cached = api.getSession().catch((e) => {
      _cached = null
      throw e
    })
    _cached.then((s) => live && setSession(s)).catch(() => {})
    return () => {
      live = false
    }
  }, [])
  return { session, isAdmin: session?.role === 'admin' }
}

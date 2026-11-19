import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { api } from '../api/client.ts'
import type { Session } from '../api/types.ts'

/**
 * Renders children once the server accepts us: auth off (local mode), a trusted
 * reverse proxy identified us, or a read-scoped API key was exchanged for a session cookie.
 */
export default function LoginGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [key, setKey] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.getSession().then(setSession).catch((e: Error) => setError(e.message))
  }, [])

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.login(key.trim())
      setSession(await api.getSession())
      setKey('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setBusy(false)
    }
  }

  if (session?.authenticated) return <>{children}</>

  return (
    <div className="flex h-screen items-center justify-center bg-py-base">
      {session === null && !error ? null : (
        <form
          onSubmit={submit}
          className="w-80 rounded-lg border border-py-border bg-py-surface p-6 flex flex-col gap-3"
        >
          <h1 className="text-sm font-mono text-py-text">Pyrometer</h1>
          <p className="text-xs text-py-muted">
            Enter an API key with the <code className="font-mono">read</code> scope.
          </p>
          <input
            type="password"
            autoComplete="off"
            autoFocus
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="pyro_…"
            className="rounded border border-py-border bg-py-elevated px-2 py-1.5 text-xs font-mono text-py-text outline-none focus:border-py-border-hi"
          />
          {error && <p className="text-xs text-py-crashed">{error}</p>}
          <button
            type="submit"
            disabled={busy || key.trim() === ''}
            className="rounded bg-py-amber px-2 py-1.5 text-xs font-mono text-black disabled:opacity-40"
          >
            {busy ? 'Checking…' : 'Sign in'}
          </button>
        </form>
      )}
    </div>
  )
}

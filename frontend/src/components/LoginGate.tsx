import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { api } from '../api/client.ts'
import type { Session } from '../api/types.ts'

const MIN_PASSWORD = 6

const inputCls =
  'rounded border border-py-border bg-py-elevated px-2 py-1.5 text-xs font-mono text-py-text outline-none focus:border-py-border-hi'

/**
 * Renders children once the server accepts us: auth off (local mode), a trusted reverse proxy
 * identified us, or the admin password / first-run setup token was accepted.
 */
export default function LoginGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [token, setToken] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.getSession().then(setSession).catch((e: Error) => setError(e.message))
  }, [])

  if (session?.authenticated) return <>{children}</>
  if (session === null && !error) return <div className="h-screen bg-py-base" />

  const setup = session?.state === 'setup_required'

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (setup) {
      if (password.length < MIN_PASSWORD) return setError(`Password must be at least ${MIN_PASSWORD} characters`)
      if (password !== confirm) return setError('Passwords do not match')
    }
    setBusy(true)
    setError(null)
    try {
      if (setup) await api.setup(token.trim(), username.trim(), password)
      else await api.login(username.trim(), password)
      setSession(await api.getSession())
      setPassword('')
      setConfirm('')
      setToken('')
      setUsername('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-screen items-center justify-center bg-py-base">
      <form
        onSubmit={submit}
        className="w-96 rounded-lg border border-py-border bg-py-surface p-6 flex flex-col gap-3"
      >
        <h1 className="text-sm font-mono text-py-text">{setup ? 'Set up Pyrometer' : 'Pyrometer'}</h1>
        {setup && (
          <p className="text-xs text-py-muted">
            Enter the one-time setup token from the server log, then choose a username and password for the first admin. Find the
            token with <code className="font-mono">docker compose logs pyrometer | grep "setup token"</code>
            , or run <code className="font-mono">pyrometer admin set-password</code> on the host.
          </p>
        )}
        {setup && (
          <input
            type="password"
            autoComplete="off"
            autoFocus
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Setup token"
            className={inputCls}
          />
        )}
        <input
          type="text"
          autoComplete="username"
          autoFocus={!setup}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="Username"
          className={inputCls}
        />
        <input
          type="password"
          autoComplete={setup ? 'new-password' : 'current-password'}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder={setup ? `Password (min ${MIN_PASSWORD} chars)` : 'Password'}
          className={inputCls}
        />
        {setup && (
          <input
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder="Confirm password"
            className={inputCls}
          />
        )}
        {error && <p className="text-xs text-py-crashed">{error}</p>}
        <button
          type="submit"
          disabled={busy || username.trim() === '' || password === '' || (setup && token.trim() === '')}
          className="rounded bg-py-amber px-2 py-1.5 text-xs font-mono text-black disabled:opacity-40"
        >
          {busy ? 'Working…' : setup ? 'Create admin' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}

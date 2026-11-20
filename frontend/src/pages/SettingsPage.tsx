import { useEffect, useState, type FormEvent } from 'react'
import { api } from '../api/client.ts'
import type { AlertFormat, AlertKind, AlertRule, ApiKey, CreatedApiKey, Project, Session, UserAccount } from '../api/types.ts'

const input =
  'rounded border border-py-border bg-py-elevated px-2 py-1.5 text-xs font-mono text-py-text outline-none focus:border-py-border-hi'
const btn = 'rounded bg-py-amber px-3 py-1.5 text-xs font-mono text-black disabled:opacity-40'
const ghost = 'rounded border border-py-border px-2 py-1 text-xs font-mono text-py-muted'
const section = 'flex flex-col gap-3'
const h2 = 'text-xs font-mono uppercase tracking-widest text-py-muted'

function msg(e: unknown, fallback: string): string {
  return e instanceof Error ? e.message : fallback
}

/** Shows a secret exactly once, with copy. */
function OneTimeSecret({ label, secret, onDone }: { label: string; secret: string; onDone: () => void }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="rounded border border-py-amber/60 bg-py-surface p-4 flex flex-col gap-2">
      <p className="text-xs text-py-text">{label} This is the only time it is shown.</p>
      <code className="select-all break-all rounded bg-py-elevated p-2 text-xs font-mono text-py-amber">{secret}</code>
      <div className="flex gap-2">
        <button
          className={ghost}
          onClick={() => navigator.clipboard.writeText(secret).then(() => setCopied(true)).catch(() => {})}
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
        <button className={ghost} onClick={onDone}>
          I've stored it
        </button>
      </div>
    </div>
  )
}

export default function SettingsPage() {
  const [session, setSession] = useState<Session | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [secret, setSecret] = useState<{ label: string; value: string } | null>(null)

  const [projects, setProjects] = useState<Project[]>([])
  const [users, setUsers] = useState<UserAccount[]>([])
  const [keys, setKeys] = useState<ApiKey[]>([])
  const [alertRules, setAlertRules] = useState<AlertRule[]>([])
  const [members, setMembers] = useState<Record<string, string[]>>({})

  const [projName, setProjName] = useState('')
  const [projVis, setProjVis] = useState<'public' | 'private'>('public')
  const [userName, setUserName] = useState('')
  const [userPw, setUserPw] = useState('')
  const [userRole, setUserRole] = useState<'admin' | 'user'>('user')
  const [keyName, setKeyName] = useState('')
  const [keyProject, setKeyProject] = useState('')
  const [aName, setAName] = useState('')
  const [aUrl, setAUrl] = useState('')
  const [aFormat, setAFormat] = useState<AlertFormat>('generic')
  const [aKind, setAKind] = useState<AlertKind>('run_crashed')
  const [aProject, setAProject] = useState('')
  const [aSeverity, setASeverity] = useState('error')
  const [aMetric, setAMetric] = useState('')
  const [aOp, setAOp] = useState('>')
  const [aThreshold, setAThreshold] = useState('')
  const [alertNote, setAlertNote] = useState<string | null>(null)
  const [curPw, setCurPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [pwDone, setPwDone] = useState(false)

  const isAdmin = session?.role === 'admin'

  async function load() {
    try {
      const s = await api.getSession()
      setSession(s)
      const ps = await api.listProjects()
      setProjects(ps)
      if (s.role === 'admin') {
        const [us, ks, als] = await Promise.all([api.listUsers(), api.listKeys(), api.listAlerts()])
        setUsers(us)
        setKeys(ks)
        setAlertRules(als)
        const entries = await Promise.all(ps.map(async (p) => [p.name, (await api.getProjectAdmin(p.name)).members ?? []] as const))
        setMembers(Object.fromEntries(entries))
      }
    } catch (e) {
      setError(msg(e, 'Failed to load'))
    }
  }
  useEffect(() => {
    load()
  }, [])

  // Wraps a mutation: clears the error, reports failures, refreshes on success.
  async function act(fn: () => Promise<void>, fail: string) {
    setError(null)
    try {
      await fn()
      await load()
    } catch (e) {
      setError(msg(e, fail))
    }
  }

  const createProject = (e: FormEvent) => {
    e.preventDefault()
    act(async () => {
      const res = await api.createProject(projName.trim(), projVis)
      if (res.key) setSecret({ label: `Private project "${res.project.name}" created. Its API key:`, value: res.key })
      setProjName('')
    }, 'Failed to create project')
  }
  const createUser = (e: FormEvent) => {
    e.preventDefault()
    act(async () => {
      await api.createUser(userName.trim(), userPw, userRole)
      setUserName('')
      setUserPw('')
    }, 'Failed to create user')
  }
  const createKey = (e: FormEvent) => {
    e.preventDefault()
    act(async () => {
      const k: CreatedApiKey = await api.createKey(keyName.trim(), keyProject || undefined)
      setSecret({ label: `${k.project ? `Key for project "${k.project}"` : 'Global key'} "${k.name}" created.`, value: k.key })
      setKeyName('')
    }, 'Failed to create key')
  }
  const createAlert = (e: FormEvent) => {
    e.preventDefault()
    act(async () => {
      await api.createAlert({
        name: aName.trim(),
        url: aUrl.trim(),
        format: aFormat,
        kind: aKind,
        project: aProject || null,
        min_severity: aKind === 'event' ? aSeverity : null,
        metric: aKind === 'metric_threshold' ? aMetric.trim() : null,
        op: aKind === 'metric_threshold' ? aOp : null,
        threshold: aKind === 'metric_threshold' ? Number(aThreshold) : null,
        enabled: true,
      })
      setAName('')
      setAUrl('')
    }, 'Failed to create alert')
  }
  const testAlert = (id: string) => {
    setAlertNote(null)
    act(async () => {
      await api.testAlert(id)
      setAlertNote('Test alert sent.')
    }, 'Test alert failed')
  }
  const describeRule = (r: AlertRule) =>
    r.kind === 'event'
      ? `event severity >= ${r.min_severity}`
      : r.kind === 'metric_threshold'
        ? `${r.metric} ${r.op} ${r.threshold}`
        : r.kind.replace('_', ' ')
  const changePassword = (e: FormEvent) => {
    e.preventDefault()
    setPwDone(false)
    act(async () => {
      await api.changePassword(curPw, newPw)
      setCurPw('')
      setNewPw('')
      setPwDone(true)
    }, 'Failed to change password')
  }
  const toggleMember = (project: string, user: string) => {
    const cur = members[project] ?? []
    const next = cur.includes(user) ? cur.filter((u) => u !== user) : [...cur, user]
    act(async () => {
      await api.updateProject(project, { members: next })
    }, 'Failed to update members')
  }

  return (
    <div className="mx-auto max-w-3xl p-8 flex flex-col gap-8">
      <h1 className="text-sm font-mono text-py-text">
        Settings <span className="text-py-faint">· {session?.user ?? '…'} ({session?.role ?? '…'})</span>
      </h1>

      {error && <p className="text-xs text-py-crashed">{error}</p>}
      {secret && <OneTimeSecret label={secret.label} secret={secret.value} onDone={() => setSecret(null)} />}

      {session?.auth === 'required' && session.role !== 'global' && session.role !== 'project' && (
        <section className={section}>
          <h2 className={h2}>Change password</h2>
          <form onSubmit={changePassword} className="flex gap-2">
            <input type="password" autoComplete="current-password" value={curPw} onChange={(e) => setCurPw(e.target.value)} placeholder="Current password" className={`${input} flex-1`} />
            <input type="password" autoComplete="new-password" value={newPw} onChange={(e) => setNewPw(e.target.value)} placeholder="New password" className={`${input} flex-1`} />
            <button type="submit" disabled={!curPw || !newPw} className={btn}>Change</button>
          </form>
          {pwDone && <p className="text-xs text-py-running">Password changed.</p>}
        </section>
      )}

      <section className={section}>
        <h2 className={h2}>Projects</h2>
        <div className="rounded border border-py-border">
          {projects.map((p) => (
            <div key={p.name} className="border-b border-py-border px-4 py-2 last:border-b-0 flex flex-col gap-2">
              <div className="flex items-center justify-between text-xs font-mono">
                <span>
                  <span className="text-py-text">{p.name}</span>
                  <span className="ml-3 text-py-faint">{p.runs ?? 0} runs · by {p.created_by}</span>
                </span>
                {isAdmin ? (
                  <select
                    value={p.visibility}
                    onChange={(e) => act(async () => { await api.updateProject(p.name, { visibility: e.target.value as 'public' | 'private' }) }, 'Failed to update project')}
                    className={input}
                  >
                    <option value="public">public</option>
                    <option value="private">private</option>
                  </select>
                ) : (
                  <span className="text-py-muted">{p.visibility}</span>
                )}
              </div>
              {isAdmin && (
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs font-mono text-py-muted">
                  <span className="text-py-faint">members:</span>
                  {users.filter((u) => u.role === 'user').length === 0 && <span className="text-py-faint">no regular users yet</span>}
                  {users.filter((u) => u.role === 'user').map((u) => (
                    <label key={u.username} className="flex items-center gap-1">
                      <input type="checkbox" checked={(members[p.name] ?? []).includes(u.username)} onChange={() => toggleMember(p.name, u.username)} />
                      {u.username}
                    </label>
                  ))}
                  {p.visibility === 'public' && <span className="text-py-faint">(public: everyone can already see it)</span>}
                </div>
              )}
            </div>
          ))}
        </div>
        {isAdmin && (
          <form onSubmit={createProject} className="flex gap-2">
            <input value={projName} onChange={(e) => setProjName(e.target.value)} placeholder="New project name" maxLength={64} className={`${input} flex-1`} />
            <select value={projVis} onChange={(e) => setProjVis(e.target.value as 'public' | 'private')} className={input}>
              <option value="public">public</option>
              <option value="private">private (gets its own key)</option>
            </select>
            <button type="submit" disabled={!projName.trim()} className={btn}>Create project</button>
          </form>
        )}
      </section>

      {isAdmin && (
        <section className={section}>
          <h2 className={h2}>Users</h2>
          <div className="rounded border border-py-border">
            {users.map((u) => (
              <div key={u.username} className="flex items-center justify-between border-b border-py-border px-4 py-2 last:border-b-0 text-xs font-mono">
                <span className="text-py-text">{u.username}</span>
                <span className="flex items-center gap-3">
                  <select
                    value={u.role}
                    onChange={(e) => act(async () => { await api.updateUser(u.username, { role: e.target.value as 'admin' | 'user' }) }, 'Failed to change role')}
                    className={input}
                  >
                    <option value="user">user</option>
                    <option value="admin">admin</option>
                  </select>
                  <button
                    className="text-py-muted"
                    onClick={() => {
                      const pw = window.prompt(`New password for ${u.username}:`)
                      if (pw) act(async () => { await api.updateUser(u.username, { password: pw }) }, 'Failed to reset password')
                    }}
                  >
                    Reset password
                  </button>
                  <button
                    className="text-py-crashed"
                    onClick={() => window.confirm(`Delete ${u.username}?`) && act(async () => { await api.deleteUser(u.username) }, 'Failed to delete user')}
                  >
                    Delete
                  </button>
                </span>
              </div>
            ))}
          </div>
          <form onSubmit={createUser} className="flex gap-2">
            <input value={userName} onChange={(e) => setUserName(e.target.value)} placeholder="Username" className={`${input} flex-1`} />
            <input type="password" autoComplete="new-password" value={userPw} onChange={(e) => setUserPw(e.target.value)} placeholder="Password" className={`${input} flex-1`} />
            <select value={userRole} onChange={(e) => setUserRole(e.target.value as 'admin' | 'user')} className={input}>
              <option value="user">user</option>
              <option value="admin">admin</option>
            </select>
            <button type="submit" disabled={!userName.trim() || !userPw} className={btn}>Add user</button>
          </form>
        </section>
      )}

      {isAdmin && (
        <section className={section}>
          <h2 className={h2}>API keys</h2>
          <p className="text-xs text-py-muted">
            A global key reads and writes every project and can register new ones. A project key is limited to
            one project. Neither can manage users, projects' settings or keys. Scripts use{' '}
            <code className="font-mono">PYROMETER_API_KEY</code>.
          </p>
          <div className="rounded border border-py-border">
            {keys.length === 0 ? (
              <p className="p-4 text-xs text-py-faint font-mono">No API keys yet.</p>
            ) : (
              keys.map((k) => (
                <div key={k.id} className="flex items-center justify-between border-b border-py-border px-4 py-2 last:border-b-0">
                  <div className="text-xs font-mono">
                    <span className="text-py-text">{k.name}</span>
                    <span className="ml-3 text-py-amber">{k.project ?? 'global'}</span>
                    <span className="ml-3 text-py-faint">{k.id} · {new Date(k.created_at * 1000).toLocaleString()}</span>
                  </div>
                  <button
                    onClick={() => window.confirm(`Revoke "${k.name}"? Anything using it will stop working.`) && act(async () => { await api.revokeKey(k.id) }, 'Failed to revoke key')}
                    className="text-xs font-mono text-py-crashed"
                  >
                    Revoke
                  </button>
                </div>
              ))
            )}
          </div>
          <form onSubmit={createKey} className="flex gap-2">
            <input value={keyName} onChange={(e) => setKeyName(e.target.value)} placeholder="Key name, e.g. the machine" maxLength={64} className={`${input} flex-1`} />
            <select value={keyProject} onChange={(e) => setKeyProject(e.target.value)} className={input}>
              <option value="">global</option>
              {projects.map((p) => (
                <option key={p.name} value={p.name}>{p.name}</option>
              ))}
            </select>
            <button type="submit" disabled={!keyName.trim()} className={btn}>Create key</button>
          </form>
        </section>
      )}
      {isAdmin && (
        <section className={section}>
          <h2 className={h2}>Alerts</h2>
          <p className="text-xs text-py-muted">
            Send a webhook when a run crashes or fails, an event or anomaly is logged, or a metric crosses a
            threshold. Webhook URLs are secrets: after saving, only the host is shown. Set{' '}
            <code className="font-mono">PYROMETER_PUBLIC_URL</code> on the server to include dashboard links.
          </p>
          {alertNote && <p className="text-xs text-py-running">{alertNote}</p>}
          <div className="rounded border border-py-border">
            {alertRules.length === 0 ? (
              <p className="p-4 text-xs text-py-faint font-mono">No alert rules yet.</p>
            ) : (
              alertRules.map((r) => (
                <div key={r.id} className="flex items-center justify-between border-b border-py-border px-4 py-2 last:border-b-0">
                  <div className="text-xs font-mono">
                    <span className={r.enabled ? 'text-py-text' : 'text-py-faint'}>{r.name}</span>
                    <span className="ml-3 text-py-amber">{describeRule(r)}</span>
                    <span className="ml-3 text-py-faint">{r.project ?? 'all projects'} · {r.format} · {r.url}</span>
                  </div>
                  <span className="flex items-center gap-3 text-xs font-mono">
                    <button className="text-py-muted" onClick={() => testAlert(r.id)}>Send test</button>
                    <button className="text-py-muted" onClick={() => act(async () => { await api.updateAlert(r.id, { enabled: !r.enabled }) }, 'Failed to update alert')}>
                      {r.enabled ? 'Disable' : 'Enable'}
                    </button>
                    <button className="text-py-crashed" onClick={() => window.confirm(`Delete "${r.name}"?`) && act(async () => { await api.deleteAlert(r.id) }, 'Failed to delete alert')}>
                      Delete
                    </button>
                  </span>
                </div>
              ))
            )}
          </div>
          <form onSubmit={createAlert} className="flex flex-col gap-2">
            <div className="flex gap-2">
              <input value={aName} onChange={(e) => setAName(e.target.value)} placeholder="Rule name" maxLength={64} className={`${input} w-40`} />
              <input type="password" autoComplete="off" value={aUrl} onChange={(e) => setAUrl(e.target.value)} placeholder="Webhook URL (secret)" className={`${input} flex-1`} />
              <select value={aFormat} onChange={(e) => setAFormat(e.target.value as AlertFormat)} className={input}>
                <option value="generic">generic JSON</option>
                <option value="ntfy">ntfy</option>
                <option value="discord">Discord</option>
                <option value="slack">Slack</option>
              </select>
            </div>
            <div className="flex gap-2">
              <select value={aKind} onChange={(e) => setAKind(e.target.value as AlertKind)} className={input}>
                <option value="run_crashed">run crashed</option>
                <option value="run_failed">run failed</option>
                <option value="event">event by severity</option>
                <option value="anomaly">anomaly / NaN</option>
                <option value="metric_threshold">metric threshold</option>
              </select>
              {aKind === 'event' && (
                <select value={aSeverity} onChange={(e) => setASeverity(e.target.value)} className={input}>
                  <option value="info">info and up</option>
                  <option value="warning">warning and up</option>
                  <option value="error">error and up</option>
                  <option value="critical">critical</option>
                </select>
              )}
              {aKind === 'metric_threshold' && (
                <>
                  <input value={aMetric} onChange={(e) => setAMetric(e.target.value)} placeholder="metric, e.g. loss" className={`${input} w-40`} />
                  <select value={aOp} onChange={(e) => setAOp(e.target.value)} className={input}>
                    {['>', '>=', '<', '<='].map((o) => <option key={o} value={o}>{o}</option>)}
                  </select>
                  <input value={aThreshold} onChange={(e) => setAThreshold(e.target.value)} placeholder="value" className={`${input} w-24`} />
                </>
              )}
              <select value={aProject} onChange={(e) => setAProject(e.target.value)} className={input}>
                <option value="">all projects</option>
                {projects.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
              </select>
              <button
                type="submit"
                disabled={!aName.trim() || !aUrl.trim() || (aKind === 'metric_threshold' && (!aMetric.trim() || aThreshold === '' || isNaN(Number(aThreshold))))}
                className={btn}
              >
                Add rule
              </button>
            </div>
          </form>
        </section>
      )}
    </div>
  )
}

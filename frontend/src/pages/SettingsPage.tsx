import { useEffect, useState, type FormEvent } from 'react'
import { api } from '../api/client.ts'
import ConfirmDialog from '../components/ConfirmDialog.tsx'
import { useWebSocket } from '../hooks/useWebSocket.ts'
import { timeAgo } from '../utils/format.ts'
import type { AlertFormat, AlertKind, AlertRule, AlertTrigger, ApiKey, CreatedApiKey, Project, Session, UserAccount } from '../api/types.ts'

const input =
  'rounded border border-py-border bg-py-elevated px-2 py-1.5 text-xs font-mono text-py-text outline-none focus:border-py-border-hi'
const btn = 'rounded bg-py-amber px-3 py-1.5 text-xs font-mono text-black disabled:opacity-40'
const ghost = 'rounded border border-py-border px-2 py-1 text-xs font-mono text-py-muted'
const section = 'flex flex-col gap-3'
const h2 = 'text-xs font-mono uppercase tracking-widest text-py-muted'

const RETENTION_TABLES = ['system_snapshots', 'logs', 'profile_events', 'param_stats', 'events', 'artifacts', 'media', 'samples', 'metrics'] as const

/** epoch seconds (keys, alert rules) -> "5m ago" */
function agoEpoch(sec: number | null | undefined): string | null {
  return sec ? timeAgo(new Date(sec * 1000).toISOString()) : null
}

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

/** Per-table "delete rows older than N days"; empty = keep forever (the default). */
function RetentionEditor({ project, onError }: { project: string; onError: (m: string) => void }) {
  const [open, setOpen] = useState(false)
  const [days, setDays] = useState<Record<string, string>>({})
  const [note, setNote] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    api.getProjectAdmin(project).then((p) => {
      const cur = p.retention?.older_than_days ?? {}
      setDays(Object.fromEntries(Object.entries(cur).map(([k, v]) => [k, String(v)])))
    }).catch((e) => onError(msg(e, 'Failed to load retention policy')))
  }, [open, project, onError])

  const [confirmRun, setConfirmRun] = useState(false)
  const invalid = Object.values(days).some((v) => v !== '' && !(Number(v) > 0 && Number(v) <= 36500))

  async function run(fn: () => Promise<string>) {
    setBusy(true)
    setNote(null)
    try {
      setNote(await fn())
    } catch (e) {
      onError(msg(e, 'Retention request failed'))
    } finally {
      setBusy(false)
    }
  }

  const save = () => run(async () => {
    const older = Object.fromEntries(Object.entries(days).filter(([, v]) => v !== '').map(([k, v]) => [k, Number(v)]))
    await api.setRetention(project, Object.keys(older).length ? { older_than_days: older } : null)
    return Object.keys(older).length ? 'Policy saved. The daily sweeper applies it.' : 'Retention is off: everything is kept.'
  })
  const runNow = () => run(async () => {
    const { deleted } = await api.runRetention(project)
    const parts = Object.entries(deleted).map(([t, n]) => `${t}: ${n}`)
    return parts.length ? `Deleted rows: ${parts.join(', ')}` : 'Nothing to delete.'
  })

  return (
    <div className="text-xs font-mono">
      <button className="text-py-muted" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {open ? '▾' : '▸'} retention policy
      </button>
      {open && (
        <div className="mt-2 flex flex-col gap-2 rounded border border-py-border p-3">
          <p className="text-py-faint">
            Delete rows older than N days, per table. Leave a field empty to keep that table forever. Runs themselves are never
            deleted. <span className="text-py-killed">Deleting metrics is permanent and thins the training curves.</span>
          </p>
          <div className="grid grid-cols-3 gap-2">
            {RETENTION_TABLES.map((t) => (
              <label key={t} className="flex items-center justify-between gap-2 text-py-muted">
                {t}
                <input
                  type="number" min={1} max={36500} placeholder="keep" aria-label={`${t}: delete rows older than days`}
                  value={days[t] ?? ''} onChange={(e) => setDays((d) => ({ ...d, [t]: e.target.value }))}
                  className={`${input} w-20`}
                />
              </label>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <button className={ghost} disabled={busy || invalid} onClick={save}>Save policy</button>
            <button className={ghost} disabled={busy} onClick={() => setConfirmRun(true)}>
              Run now
            </button>
            {invalid && <span className="text-py-crashed">Days must be between 1 and 36500.</span>}
            {note && <span role="status" className="text-py-running">{note}</span>}
          </div>
        </div>
      )}
      {confirmRun && (
        <ConfirmDialog
          title={`Apply the retention policy to ${project} now?`}
          confirmLabel="Delete matching rows"
          danger
          onCancel={() => setConfirmRun(false)}
          onConfirm={async () => {
            setConfirmRun(false)
            await runNow()
          }}
        >
          <p>The saved policy is applied immediately: rows older than the configured number of days are deleted permanently and the affected run summaries are recomputed. This cannot be undone.</p>
        </ConfirmDialog>
      )}
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
  const [aTriggers, setATriggers] = useState<AlertTrigger[]>([])
  const [aProject, setAProject] = useState('')
  const [aSeverity, setASeverity] = useState('error')
  const [aMetric, setAMetric] = useState('')
  const [aOp, setAOp] = useState('>')
  const [aThreshold, setAThreshold] = useState('')
  const [alertNote, setAlertNote] = useState<string | null>(null)
  const [curPw, setCurPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [pwDone, setPwDone] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [showArchived, setShowArchived] = useState(false)
  const [deleting, setDeleting] = useState<Project | null>(null)
  const [cascade, setCascade] = useState(false)

  const isAdmin = session?.role === 'admin'

  async function load() {
    try {
      const s = await api.getSession()
      setSession(s)
      const ps = await api.listProjects({ include_archived: showArchived })
      setProjects(ps)
      if (s.role === 'admin') {
        const [us, ks, als] = await Promise.all([api.listUsers(), api.listKeys(), api.listAlerts()])
        setUsers([...us].sort((a, b) => a.username.localeCompare(b.username)))
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
  }, [showArchived])

  useWebSocket((m) => {
    if (m.type === 'project_updated' || m.type === 'project_deleted') load()
  })
  const activeProjects = projects.filter((p) => !p.archived)

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
        project: aProject || null,
        triggers: aTriggers,
        enabled: true,
      })
      setAName('')
      setAUrl('')
      setATriggers([])
    }, 'Failed to create alert')
  }
  const testAlert = (id: string) => {
    setAlertNote(null)
    act(async () => {
      await api.testAlert(id)
      setAlertNote('Test alert sent.')
    }, 'Test alert failed')
  }
  const describeTrigger = (t: AlertTrigger) =>
    t.kind === 'event'
      ? `event severity >= ${t.min_severity}`
      : t.kind === 'metric_threshold'
        ? `${t.metric} ${t.op} ${t.threshold}`
        : t.kind.replace('_', ' ')
  const describeRule = (r: AlertRule) => r.triggers.map(describeTrigger).join(' | ')
  const draftValid = aKind !== 'metric_threshold' || (aMetric.trim() !== '' && aThreshold !== '' && !isNaN(Number(aThreshold)))
  const addTrigger = () => {
    setATriggers((ts) => [
      ...ts,
      aKind === 'event'
        ? { kind: aKind, min_severity: aSeverity }
        : aKind === 'metric_threshold'
          ? { kind: aKind, metric: aMetric.trim(), op: aOp, threshold: Number(aThreshold) }
          : { kind: aKind },
    ])
  }
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

      {error && <p role="alert" className="text-xs text-py-crashed">{error}</p>}
      {notice && <p role="status" className="text-xs text-py-running">{notice}</p>}
      {secret && <OneTimeSecret label={secret.label} secret={secret.value} onDone={() => setSecret(null)} />}

      {session?.auth === 'required' && session.role !== 'global' && session.role !== 'project' && (
        <section className={section}>
          <h2 className={h2}>Change password</h2>
          <form onSubmit={changePassword} className="flex flex-wrap gap-2">
            <input type="password" autoComplete="current-password" value={curPw} onChange={(e) => setCurPw(e.target.value)} placeholder="Current password" className={`${input} flex-1`} />
            <input type="password" autoComplete="new-password" value={newPw} onChange={(e) => setNewPw(e.target.value)} placeholder="New password" className={`${input} flex-1`} />
            <button type="submit" disabled={!curPw || !newPw} className={btn}>Change</button>
          </form>
          {pwDone && <p className="text-xs text-py-running">Password changed.</p>}
        </section>
      )}

      <section className={section}>
        <h2 className={h2}>Projects</h2>
        <label className="flex items-center gap-2 text-xs font-mono text-py-muted">
          <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
          Show archived projects
        </label>
        <div className="rounded border border-py-border">
          {projects.map((p) => (
            <div key={p.name} className="border-b border-py-border px-4 py-2 last:border-b-0 flex flex-col gap-2" style={{ opacity: p.archived ? 0.65 : 1 }}>
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs font-mono">
                <span>
                  <span className="text-py-text">{p.name}</span>
                  {p.archived && <span className="ml-2 text-py-killed">archived</span>}
                  <span className="ml-3 text-py-faint">
                    {p.runs ?? 0} runs{p.archived_runs ? ` (${p.archived_runs} archived)` : ''} · by {p.created_by} ·{' '}
                    {p.last_activity ? `active ${timeAgo(p.last_activity)}` : 'no activity'}
                  </span>
                </span>
                <span className="flex items-center gap-3">
                  <a href={api.projectExportUrl(p.name)} download className="text-py-muted" title="Download every run of this project as Parquet files in a zip">
                    Export
                  </a>
                  {isAdmin && p.name !== 'default' && (
                    <>
                      <button className="text-py-muted" onClick={() => act(async () => { await api.archiveProject(p.name, !p.archived) }, 'Failed to update project')}>
                        {p.archived ? 'Unarchive' : 'Archive'}
                      </button>
                      <button className="text-py-crashed" onClick={() => { setCascade(false); setDeleting(p) }}>Delete…</button>
                    </>
                  )}
                  {isAdmin ? (
                    <select
                      aria-label={`Visibility of ${p.name}`}
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
                </span>
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
              {isAdmin && <RetentionEditor project={p.name} onError={setError} />}
            </div>
          ))}
        </div>
        {isAdmin && (
          <form onSubmit={createProject} className="flex flex-wrap gap-2">
            <input value={projName} onChange={(e) => setProjName(e.target.value)} placeholder="New project name" maxLength={64} className={`${input} flex-1`} />
            <select value={projVis} onChange={(e) => setProjVis(e.target.value as 'public' | 'private')} className={input}>
              <option value="public">public</option>
              <option value="private">private (gets its own key)</option>
            </select>
            <button type="submit" disabled={!projName.trim()} className={btn}>Create project</button>
          </form>
        )}
      </section>

      {deleting && (
        <ConfirmDialog
          title={`Delete project ${deleting.name}?`}
          confirmLabel="Delete project"
          requireText={deleting.name}
          danger
          onCancel={() => setDeleting(null)}
          onConfirm={async () => {
            const res = await api.deleteProject(deleting.name, cascade)
            setDeleting(null)
            setError(null)
            setNotice(`Deleted ${res.deleted}: ${res.runs_deleted} runs, ${res.keys_revoked} API keys revoked, ${res.alert_rules_removed} alert rules removed.`)
            await load()
          }}
        >
          <p>
            Deleting a project is permanent. It also <b>revokes every API key bound to it</b> (scripts using them stop working, and a
            project re-created under the same name inherits nothing), removes its members and deletes the alert rules scoped to it.
          </p>
          {(deleting.runs ?? 0) > 0 ? (
            <label className="flex items-start gap-2 text-py-text">
              <input type="checkbox" checked={cascade} onChange={(e) => setCascade(e.target.checked)} className="mt-0.5" />
              <span>Also delete its {deleting.runs} run{deleting.runs === 1 ? '' : 's'} and all their data. Without this the server refuses while runs exist.</span>
            </label>
          ) : (
            <p>The project has no runs.</p>
          )}
        </ConfirmDialog>
      )}

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
          <form onSubmit={createUser} className="flex flex-wrap gap-2">
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
                    <span className="ml-3 text-py-faint">
                      {k.id} · created {new Date(k.created_at * 1000).toLocaleDateString()} ·{' '}
                      {agoEpoch(k.last_used_at) ? `last used ${agoEpoch(k.last_used_at)}` : 'never used'}
                    </span>
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
          <form onSubmit={createKey} className="flex flex-wrap gap-2">
            <input value={keyName} onChange={(e) => setKeyName(e.target.value)} placeholder="Key name, e.g. the machine" maxLength={64} className={`${input} flex-1`} />
            <select value={keyProject} onChange={(e) => setKeyProject(e.target.value)} className={input}>
              <option value="">global</option>
              {activeProjects.map((p) => (
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
                <div key={r.id} className="flex items-center justify-between gap-4 border-b border-py-border px-4 py-2 last:border-b-0">
                  <div className="min-w-0 break-words text-xs font-mono">
                    <span className={r.enabled ? 'text-py-text' : 'text-py-faint'}>{r.name}</span>
                    <span className="ml-3 text-py-amber">{describeRule(r)}</span>
                    <span className="ml-3 text-py-faint">
                      {r.project ?? 'all projects'} · {r.format} · {r.url}
                      {agoEpoch(r.updated_at) ? ` · updated ${agoEpoch(r.updated_at)}` : ''}
                    </span>
                  </div>
                  <span className="flex shrink-0 items-center gap-3 whitespace-nowrap text-xs font-mono">
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
            <div className="flex flex-wrap gap-2">
              <input value={aName} onChange={(e) => setAName(e.target.value)} placeholder="Rule name" maxLength={64} className={`${input} w-40`} />
              <input type="password" autoComplete="off" value={aUrl} onChange={(e) => setAUrl(e.target.value)} placeholder="Webhook URL (secret)" className={`${input} flex-1`} />
              <select value={aFormat} onChange={(e) => setAFormat(e.target.value as AlertFormat)} className={input}>
                <option value="generic">generic JSON</option>
                <option value="ntfy">ntfy</option>
                <option value="discord">Discord</option>
                <option value="slack">Slack</option>
              </select>
            </div>
            <div className="flex flex-wrap gap-2">
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
              <button type="button" disabled={!draftValid} onClick={addTrigger} className={btn}>
                Add trigger
              </button>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs font-mono">
              <span className="text-py-muted">Triggers (any match fires):</span>
              {aTriggers.length === 0 && <span className="text-py-faint">none yet</span>}
              {aTriggers.map((t, i) => (
                <button
                  key={i}
                  type="button"
                  title="Remove"
                  className="rounded border border-py-border px-2 py-0.5 text-py-amber"
                  onClick={() => setATriggers((ts) => ts.filter((_, j) => j !== i))}
                >
                  {describeTrigger(t)} x
                </button>
              ))}
            </div>
            <div className="flex gap-2">
              <select value={aProject} onChange={(e) => setAProject(e.target.value)} className={input}>
                <option value="">all projects</option>
                {activeProjects.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
              </select>
              <button type="submit" disabled={!aName.trim() || !aUrl.trim() || aTriggers.length === 0} className={btn}>
                Add rule
              </button>
            </div>
          </form>
        </section>
      )}
    </div>
  )
}

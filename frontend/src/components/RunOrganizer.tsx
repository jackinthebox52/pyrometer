import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, PINS_CHANGED } from '../api/client.ts'
import type { Project, Run } from '../api/types.ts'
import { parseTags } from '../utils/runList.ts'
import ConfirmDialog from './ConfirmDialog.tsx'

const input =
  'rounded border border-py-border bg-py-elevated px-2 py-1 text-xs font-mono text-py-text outline-none focus:border-py-border-hi'
const ghost = 'rounded border border-py-border bg-py-elevated px-2 py-1 text-xs font-mono text-py-muted disabled:opacity-40'

interface CommonProps {
  run: Run
  /** Dashboard admins are the only browser users who may edit run metadata (docs/auth.md). */
  isAdmin: boolean
  /** Re-read the run after a change. */
  onChanged: () => void
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : 'Failed'
}

/** Run name (inline rename), group and tag editor. Read-only for everyone but admins. */
export function RunTitle({ run, isAdmin, onChanged }: CommonProps) {
  const [editing, setEditing] = useState<'name' | 'group' | null>(null)
  const [draft, setDraft] = useState('')
  const [tagDraft, setTagDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (editing) inputRef.current?.focus()
  }, [editing])

  async function patch(p: Parameters<typeof api.updateRun>[1]) {
    setBusy(true)
    setError(null)
    try {
      await api.updateRun(run.run_id, p)
      onChanged()
      setEditing(null)
      return true
    } catch (e) {
      setError(errMsg(e))
      return false
    } finally {
      setBusy(false)
    }
  }

  function start(which: 'name' | 'group') {
    setDraft(which === 'name' ? run.name : (run.group ?? ''))
    setEditing(which)
    setError(null)
  }

  function submitEdit(e: FormEvent) {
    e.preventDefault()
    if (editing === 'name') {
      if (!draft.trim()) return setError('Name cannot be empty')
      if (draft.trim() === run.name) return setEditing(null)
      patch({ name: draft.trim() })
    } else {
      patch({ group: draft.trim() || null })
    }
  }

  const editForm = (label: string, maxLength: number, cls = '') => (
    <form onSubmit={submitEdit} className="flex items-center gap-2">
      <input
        ref={inputRef}
        aria-label={label}
        value={draft}
        maxLength={maxLength}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => e.key === 'Escape' && setEditing(null)}
        className={`${input} ${cls}`}
      />
      <button type="submit" disabled={busy} className={ghost}>Save</button>
      <button type="button" onClick={() => setEditing(null)} className={ghost}>Cancel</button>
    </form>
  )

  return (
    <div>
      {editing === 'name' ? (
        <div className="mb-1.5">{editForm('Run name', 200, 'w-80 text-base')}</div>
      ) : (
        <h1 className="text-lg font-semibold mb-1.5 flex items-center gap-2" style={{ color: '#e8eaf0' }}>
          {run.name}
          {run.archived && <span className="rounded border border-py-killed/60 px-1.5 py-0.5 text-xs font-mono font-normal text-py-killed">archived</span>}
          {isAdmin && (
            <button onClick={() => start('name')} aria-label="Rename run" title="Rename" className="text-xs font-mono font-normal text-py-faint hover:text-py-muted">
              rename
            </button>
          )}
        </h1>
      )}

      <div className="flex flex-wrap items-center gap-1.5 mb-2 text-xs font-mono">
        <span className="text-py-faint">{run.project}</span>
        <span className="text-py-faint">·</span>
        {editing === 'group' ? (
          editForm('Group', 128)
        ) : (
          <span className="text-py-muted">
            group: {run.group ?? <span className="text-py-faint">none</span>}
            {isAdmin && (
              <button onClick={() => start('group')} aria-label="Edit group" className="ml-2 text-py-faint hover:text-py-muted">edit</button>
            )}
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1.5" aria-label="Tags">
        {run.tags.map((tag) => (
          <span key={tag} className="inline-flex items-center gap-1 text-xs font-mono px-2 py-0.5 rounded" style={{ backgroundColor: '#1a1d24', color: '#8b8fa8', border: '1px solid #252830' }}>
            #{tag}
            {isAdmin && (
              <button
                aria-label={`Remove tag ${tag}`}
                disabled={busy}
                onClick={() => patch({ tags: run.tags.filter((t) => t !== tag) })}
                className="text-py-faint hover:text-py-crashed"
              >
                ×
              </button>
            )}
          </span>
        ))}
        {isAdmin && (
          <form
            onSubmit={(e) => {
              e.preventDefault()
              const add = parseTags(tagDraft)
              if (add.length === 0) return
              patch({ tags: [...new Set([...run.tags, ...add])] }).then((ok) => ok && setTagDraft(''))
            }}
          >
            <input aria-label="Add tag" placeholder="+ tag" value={tagDraft} onChange={(e) => setTagDraft(e.target.value)} className={`${input} w-24`} />
          </form>
        )}
      </div>
      {error && <p role="alert" className="mt-1 text-xs text-py-crashed">{error}</p>}
    </div>
  )
}

/** Notes block with an inline editor for admins; hidden when empty for everyone else. */
export function RunNotes({ run, isAdmin, onChanged }: CommonProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!run.notes && !isAdmin) return null

  async function save() {
    setBusy(true)
    setError(null)
    try {
      await api.updateRun(run.run_id, { notes: draft })
      onChanged()
      setEditing(false)
    } catch (e) {
      setError(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="p-4 rounded-lg mb-6 text-sm leading-relaxed" style={{ backgroundColor: '#111318', border: '1px solid #252830', color: '#8b8fa8' }}>
      {editing ? (
        <div className="flex flex-col gap-2">
          <textarea
            aria-label="Run notes"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === 'Escape' && setEditing(false)}
            rows={5}
            autoFocus
            className={`${input} w-full`}
          />
          {error && <p role="alert" className="text-xs text-py-crashed">{error}</p>}
          <div className="flex gap-2">
            <button onClick={save} disabled={busy} className={ghost}>Save notes</button>
            <button onClick={() => setEditing(false)} className={ghost}>Cancel</button>
          </div>
        </div>
      ) : (
        <div className="flex items-start justify-between gap-4">
          <div className="whitespace-pre-wrap">{run.notes || <span className="text-py-faint">No notes.</span>}</div>
          {isAdmin && (
            <button
              onClick={() => { setDraft(run.notes ?? ''); setEditing(true) }}
              aria-label="Edit notes"
              className="text-xs font-mono text-py-faint hover:text-py-muted"
            >
              edit
            </button>
          )}
        </div>
      )}
    </div>
  )
}

type Dialog = 'move' | 'delete' | null

/** Pin toggle, Parquet export and (admins) the archive / move / delete menu. */
export function RunActions({ run, isAdmin, canPin, onChanged }: CommonProps & { canPin: boolean }) {
  const navigate = useNavigate()
  const [pinned, setPinned] = useState(!!run.pinned)
  const [menu, setMenu] = useState(false)
  const [dialog, setDialog] = useState<Dialog>(null)
  const [projects, setProjects] = useState<Project[]>([])
  const [target, setTarget] = useState('')
  const [force, setForce] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => setPinned(!!run.pinned), [run.pinned, run.run_id])

  useEffect(() => {
    if (!menu) return
    const close = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenu(false)
    }
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setMenu(false)
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', esc)
    }
  }, [menu])

  async function togglePin() {
    setError(null)
    try {
      if (pinned) await api.unpinRun(run.run_id)
      else await api.pinRun(run.run_id)
      setPinned(!pinned)
      window.dispatchEvent(new Event(PINS_CHANGED))
    } catch (e) {
      setError(errMsg(e))
    }
  }

  async function archive() {
    setMenu(false)
    setError(null)
    try {
      await api.archiveRun(run.run_id, !run.archived)
      onChanged()
    } catch (e) {
      setError(errMsg(e))
    }
  }

  function openMove() {
    setMenu(false)
    setTarget('')
    setForce(false)
    setDialog('move')
    api.listProjects().then(setProjects).catch(() => {})
  }

  const itemCls = 'block w-full px-3 py-1.5 text-left text-xs font-mono text-py-muted hover:bg-py-elevated'

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-2">
        {canPin && (
          <button onClick={togglePin} aria-pressed={pinned} className={ghost} title={pinned ? 'Unpin from the top of the sidebar' : 'Pin to the top of the sidebar'}>
            {pinned ? '★ pinned' : '☆ pin'}
          </button>
        )}
        <a href={api.runExportUrl(run.run_id)} download className={ghost} title="Download every table of this run as a zip of Parquet files">
          export
        </a>
        {isAdmin && (
          <div className="relative" ref={menuRef}>
            <button onClick={() => setMenu((v) => !v)} aria-haspopup="menu" aria-expanded={menu} className={ghost}>
              manage ▾
            </button>
            {menu && (
              <div role="menu" className="absolute right-0 z-20 mt-1 w-44 rounded border border-py-border-hi bg-py-surface py-1 shadow-lg">
                <button role="menuitem" className={itemCls} onClick={archive}>{run.archived ? 'Unarchive' : 'Archive'}</button>
                <button role="menuitem" className={itemCls} onClick={openMove}>Move to project…</button>
                <button role="menuitem" className={`${itemCls} text-py-crashed`} onClick={() => { setMenu(false); setDialog('delete') }}>Delete…</button>
              </div>
            )}
          </div>
        )}
      </div>
      {error && <p role="alert" className="text-xs text-py-crashed">{error}</p>}

      {dialog === 'move' && (
        <ConfirmDialog
          title="Move run to another project"
          confirmLabel="Move"
          onCancel={() => setDialog(null)}
          onConfirm={async () => {
            if (!target) throw new Error('Pick a project')
            await api.moveRun(run.run_id, target, force)
            setDialog(null)
            onChanged()
          }}
        >
          <p>Moving changes who can see and write this run. API keys bound to <b>{run.project}</b> lose access to it.</p>
          <label className="flex flex-col gap-1">
            Target project
            <select value={target} onChange={(e) => setTarget(e.target.value)} className={input}>
              <option value="">Select…</option>
              {projects.filter((p) => p.name !== run.project && !p.archived).map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
            </select>
          </label>
          {run.status === 'running' && (
            <label className="flex items-start gap-2">
              <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} className="mt-0.5" />
              <span>This run is still running. Move it anyway (the training script's key may then be refused).</span>
            </label>
          )}
        </ConfirmDialog>
      )}
      {dialog === 'delete' && (
        <ConfirmDialog
          title="Delete run permanently?"
          confirmLabel="Delete run"
          requireText={run.name}
          danger
          onCancel={() => setDialog(null)}
          onConfirm={async () => {
            await api.deleteRun(run.run_id)
            setDialog(null)
            navigate('/')
          }}
        >
          <p>This removes the run and every metric, log, profile, image and sample recorded for it. It cannot be undone. Archive it instead to just hide it.</p>
        </ConfirmDialog>
      )}
    </div>
  )
}

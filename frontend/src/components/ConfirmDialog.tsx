import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

interface Props {
  title: string
  children: ReactNode
  confirmLabel: string
  /** When set, the confirm button stays disabled until this exact text is typed. */
  requireText?: string
  danger?: boolean
  onConfirm: () => Promise<void>
  onCancel: () => void
}

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]'

/** Modal confirm: Esc cancels, Tab stays inside, focus starts on the typed-confirm field (or Cancel). */
export default function ConfirmDialog({ title, children, confirmLabel, requireText, danger, onConfirm, onCancel }: Props) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const boxRef = useRef<HTMLFormElement>(null)
  const previous = useRef<Element | null>(document.activeElement)

  useEffect(() => {
    const box = boxRef.current
    const first = box?.querySelector<HTMLElement>('input') ?? box?.querySelector<HTMLElement>('button[data-cancel]')
    first?.focus()
    const prev = previous.current
    return () => {
      if (prev instanceof HTMLElement) prev.focus()
    }
  }, [])

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') {
      e.stopPropagation()
      if (!busy) onCancel()
    } else if (e.key === 'Tab') {
      const els = [...(boxRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])]
      if (els.length === 0) return
      const first = els[0]
      const last = els[els.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (requireText !== undefined && typed !== requireText) return
    setBusy(true)
    setError(null)
    try {
      await onConfirm()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed')
      setBusy(false)
    }
  }

  const blocked = busy || (requireText !== undefined && typed !== requireText)

  return createPortal(
    <div
      className="fixed inset-0 z-[300] flex items-center justify-center bg-black/70 p-6"
      onMouseDown={() => !busy && onCancel()}
    >
      <form
        ref={boxRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
        onSubmit={submit}
        className="w-full max-w-md rounded-lg border border-py-border-hi bg-py-surface p-5 flex flex-col gap-3"
      >
        <h2 className="text-sm font-semibold text-py-text">{title}</h2>
        <div className="text-xs leading-relaxed text-py-muted flex flex-col gap-2">{children}</div>
        {requireText !== undefined && (
          <label className="flex flex-col gap-1 text-xs text-py-muted">
            Type <code className="font-mono text-py-text">{requireText}</code> to confirm
            <input
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              className="rounded border border-py-border bg-py-elevated px-2 py-1.5 text-xs font-mono text-py-text outline-none focus:border-py-border-hi"
            />
          </label>
        )}
        {error && <p role="alert" className="text-xs text-py-crashed">{error}</p>}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            data-cancel
            onClick={onCancel}
            disabled={busy}
            className="rounded border border-py-border px-3 py-1.5 text-xs font-mono text-py-muted"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={blocked}
            className={`rounded px-3 py-1.5 text-xs font-mono disabled:opacity-40 ${danger ? 'bg-py-crashed text-white' : 'bg-py-amber text-black'}`}
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  )
}

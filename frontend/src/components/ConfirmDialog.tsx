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
      className="fixed inset-0 z-[300] flex items-center justify-center bg-py-n0/80 p-6"
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
        className="w-full max-w-md rounded border border-py-border-hi bg-py-surface p-5 flex flex-col gap-3"
      >
        <h2 className="text-[15px] font-semibold text-py-text">{title}</h2>
        <div className="text-[13px] leading-relaxed text-py-muted flex flex-col gap-2">{children}</div>
        {requireText !== undefined && (
          <label className="flex flex-col gap-1 text-[13px] text-py-muted">
            Type <code className="font-mono text-py-text">{requireText}</code> to confirm
            <input
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              className="h-8 rounded border border-py-border-hi bg-py-elevated px-2 font-mono text-[13px] text-py-text"
            />
          </label>
        )}
        {error && <p role="alert" className="text-[13px] text-py-red">{error}</p>}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            data-cancel
            onClick={onCancel}
            disabled={busy}
            className="h-8 rounded border border-py-border-hi bg-py-surface px-3 text-[13px] text-py-soft transition-colors hover:bg-py-elevated hover:text-py-text disabled:cursor-not-allowed disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={blocked}
            className={`h-8 rounded px-3 text-[13px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${danger ? 'border border-py-red bg-py-red text-py-n0 hover:bg-py-red/85' : 'bg-py-n8 text-py-base hover:bg-py-text'}`}
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  )
}

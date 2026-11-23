import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import Icon from './Icon.tsx'
import { N } from '../theme.ts'

interface Props {
  onClose: () => void
  children: React.ReactNode
}

/** Full-screen overlay for an expanded chart. Escape or a click on the backdrop closes it. */
export default function ExpandModal({ onClose, children }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return createPortal(
    <div
      onClick={onClose}
      className="fixed inset-0 z-[200] flex items-center justify-center p-4 sm:p-8 "
      style={{ backgroundColor: `color-mix(in srgb, ${N.n1} 85%, transparent)` }}
    >
      <div
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
        className="relative w-[92vw] max-w-[1400px] max-h-[88vh] overflow-y-auto rounded-sm border border-py-border-hi bg-py-base"
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          title="Close (Esc)"
          className="absolute right-2 top-2 z-10 inline-flex h-7 w-7 items-center justify-center rounded-sm border border-py-border-hi bg-py-surface text-py-soft transition-colors duration-150 ease-out hover:bg-py-elevated hover:text-py-text"
        >
          <Icon name="x" size={14} />
        </button>
        {children}
      </div>
    </div>,
    document.body,
  )
}

/** Small expand-icon button rendered in card headers. */
export function ExpandButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title="Expand"
      aria-label="Expand chart"
      className="inline-flex h-6 w-6 items-center justify-center rounded-sm text-py-muted transition-colors duration-150 ease-out hover:bg-py-elevated hover:text-py-text"
    >
      <Icon name="expand" size={14} />
    </button>
  )
}

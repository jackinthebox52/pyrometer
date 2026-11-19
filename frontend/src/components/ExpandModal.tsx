import { useEffect } from 'react'
import { createPortal } from 'react-dom'

interface Props {
  onClose: () => void
  children: React.ReactNode
}

export default function ExpandModal({ onClose, children }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return createPortal(
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 200,
        background: 'rgba(0,0,0,0.80)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 32,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: '92vw',
          maxWidth: 1400,
          maxHeight: '88vh',
          overflowY: 'auto',
          borderRadius: 10,
          border: '1px solid #353840',
          background: '#0a0b0e',
        }}
      >
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
      onClick={onClick}
      title="Expand"
      style={{
        background: 'none',
        border: 'none',
        cursor: 'pointer',
        padding: '2px 4px',
        color: '#4a4e60',
        lineHeight: 1,
        borderRadius: 4,
      }}
      onMouseEnter={(e) => ((e.currentTarget as HTMLButtonElement).style.color = '#8b8fa8')}
      onMouseLeave={(e) => ((e.currentTarget as HTMLButtonElement).style.color = '#4a4e60')}
    >
      {/* Simple "expand" SVG — two diagonal arrows */}
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
        <path d="M1 4.5V1h3.5M11 4.5V1H7.5M1 7.5V11h3.5M11 7.5V11H7.5" />
      </svg>
    </button>
  )
}

import type { RunStatus } from '../api/types.ts'

const CONFIG: Record<RunStatus, { color: string; label: string; pulse: boolean }> = {
  running: { color: '#22c55e', label: 'Running', pulse: true },
  completed: { color: '#60a5fa', label: 'Completed', pulse: false },
  failed: { color: '#f97316', label: 'Failed', pulse: false },
  crashed: { color: '#ef4444', label: 'Crashed', pulse: false },
  killed: { color: '#eab308', label: 'Killed', pulse: false },
}

interface Props {
  status: RunStatus
  size?: 'sm' | 'md'
  /** Show only the dot, no label text */
  compact?: boolean
}

export default function StatusBadge({ status, size = 'md', compact = false }: Props) {
  const { color, label, pulse } = CONFIG[status]
  const dotPx = size === 'sm' || compact ? 6 : 8

  return (
    <span
      className={`inline-flex items-center gap-1.5 font-mono ${size === 'sm' ? 'text-xs' : 'text-sm'}`}
      style={{ color }}
    >
      <span
        className={pulse ? 'status-pulse' : ''}
        style={{
          display: 'inline-block',
          width: dotPx,
          height: dotPx,
          borderRadius: '50%',
          backgroundColor: color,
          flexShrink: 0,
        }}
      />
      {!compact && label}
    </span>
  )
}

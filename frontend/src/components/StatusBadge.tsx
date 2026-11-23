import type { RunStatus } from '../api/types.ts'

const CONFIG: Record<RunStatus, { chip: string; label: string; pulse: boolean }> = {
  running: { chip: 'bg-py-running', label: 'Running', pulse: true },
  completed: { chip: 'bg-py-steel', label: 'Completed', pulse: false },
  failed: { chip: 'bg-py-red', label: 'Failed', pulse: false },
  crashed: { chip: 'bg-py-red', label: 'Crashed', pulse: false },
  killed: { chip: 'bg-py-muted', label: 'Killed', pulse: false },
}

interface Props {
  status: RunStatus
  size?: 'sm' | 'md'
  /** Show only the chip, no label text */
  compact?: boolean
}

/** Run status: a small square heat chip plus a sentence-case label. */
export default function StatusBadge({ status, size = 'md', compact = false }: Props) {
  const { chip, label, pulse } = CONFIG[status]
  const chipEl = (
    <span
      aria-hidden="true"
      className={`inline-block h-2 w-2 shrink-0 rounded-[1px] ${chip}${pulse ? ' status-pulse' : ''}`}
    />
  )
  if (compact) {
    return (
      <span className="inline-flex items-center" title={label} role="img" aria-label={label}>
        {chipEl}
      </span>
    )
  }
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-[2px] border bg-py-surface px-1.5 text-py-soft border-py-border ${
        size === 'sm' ? 'py-px text-[11px]' : 'py-0.5 text-xs'
      }`}
    >
      {chipEl}
      {label}
    </span>
  )
}

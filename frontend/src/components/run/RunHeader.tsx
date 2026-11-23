import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../api/client.ts'
import type { Run } from '../../api/types.ts'
import Icon from '../Icon.tsx'
import StatusBadge from '../StatusBadge.tsx'
import { RunActions, RunTitle } from '../RunOrganizer.tsx'
import { duration, timeAgo } from '../../utils/format.ts'
import { btn, btnPrimary, copyText } from './shared.tsx'

type CopyState = 'idle' | 'busy' | 'done' | 'failed'

/** A button that shows a transient confirmation for 1.5 s after `action` resolves true. */
function useCopyState(): [CopyState, (action: () => Promise<boolean>) => void] {
  const [state, setState] = useState<CopyState>('idle')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => void (timer.current && clearTimeout(timer.current)), [])
  const run = (action: () => Promise<boolean>) => {
    setState('busy')
    action()
      .catch(() => false)
      .then((ok) => {
        setState(ok ? 'done' : 'failed')
        if (timer.current) clearTimeout(timer.current)
        timer.current = setTimeout(() => setState('idle'), 1500)
      })
  }
  return [state, run]
}

interface Props {
  run: Run
  isAdmin: boolean
  canPin: boolean
  lastStep: number | null
  onChanged: () => void
}

export default function RunHeader({ run, isAdmin, canPin, lastStep, onChanged }: Props) {
  const navigate = useNavigate()
  const [llm, copyLlm] = useCopyState()
  const [idCopy, copyId] = useCopyState()

  return (
    <header className="mb-4 flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
      <div className="min-w-0">
        <RunTitle run={run} isAdmin={isAdmin} onChanged={onChanged} />
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          <StatusBadge status={run.status} />
          <span className="num text-xs text-py-muted">
            {timeAgo(run.created_at)}
            {' · '}
            {duration(run.created_at, run.finished_at ?? new Date().toISOString())}
            {lastStep != null && ` · step ${lastStep.toLocaleString()}`}
          </span>
        </div>
      </div>

      <div className="flex w-full flex-col items-end gap-2 sm:w-auto max-sm:items-start">
        <div className="flex flex-wrap items-center justify-end gap-2 max-sm:w-full max-sm:justify-start">
          <button
            type="button"
            className={`${btnPrimary} max-sm:w-full max-sm:justify-center`}
            onClick={() => navigate(`/compare?ids=${run.run_id}`)}
            title="Open the compare view with this run, then add more from the sidebar"
          >
            <Icon name="compare" size={14} />
            Compare
          </button>
          <button
            type="button"
            className={`${btn} max-sm:gap-1 max-sm:px-2`}
            disabled={llm === 'busy'}
            onClick={() => copyLlm(async () => copyText(await api.getLlmSummary(run.run_id)))}
            title="Copy a plain-text summary of this run, for pasting into an LLM"
            aria-live="polite"
          >
            <Icon name={llm === 'done' ? 'check' : 'copy'} size={14} />
            {llm === 'done' ? 'Copied' : llm === 'failed' ? 'Copy failed' : 'Copy for LLM'}
          </button>
          <RunActions run={run} isAdmin={isAdmin} canPin={canPin} onChanged={onChanged} />
        </div>
        <button
          type="button"
          className="num inline-flex h-6 items-center gap-1.5 rounded border border-py-border bg-py-surface px-2 text-[11px] text-py-muted transition-colors hover:bg-py-elevated hover:text-py-text"
          title={`Copy run id ${run.run_id}`}
          aria-label={`Copy run id ${run.run_id}`}
          onClick={() => copyId(() => copyText(run.run_id))}
        >
          {run.run_id.slice(0, 13)}
          <Icon name={idCopy === 'done' ? 'check' : 'copy'} size={12} />
        </button>
      </div>
    </header>
  )
}

import type { Artifact, Run, RunLog } from '../../api/types.ts'
import { formatBytes } from '../../utils/format.ts'
import HyperparamTable from '../HyperparamTable.tsx'
import LogViewer from '../LogViewer.tsx'
import MediaGallery from '../MediaGallery.tsx'
import SamplesViewer from '../SamplesViewer.tsx'
import { RunNotes } from '../RunOrganizer.tsx'
import { Heading, Note } from './shared.tsx'

export function LogsTab({ logs }: { logs: RunLog[] }) {
  return (
    <div>
      <Heading>Console</Heading>
      <LogViewer logs={logs} />
    </div>
  )
}

export function MediaTab({ run }: { run: Run }) {
  // Both components render nothing (heading included) when the run logged none of their kind.
  return (
    <div className="flex flex-col gap-6">
      <MediaGallery runId={run.run_id} />
      <SamplesViewer runs={[{ runId: run.run_id, label: run.name }]} />
    </div>
  )
}

export function ArtifactsTab({ artifacts }: { artifacts: Artifact[] }) {
  return (
    <div>
      <Heading>Artifacts</Heading>
      <div className="overflow-x-auto rounded border border-py-border bg-py-surface">
        <table className="w-full border-collapse text-[13px]">
          <thead>
            <tr className="border-b border-py-border text-left">
              {['Type', 'Path', 'Size', 'Step'].map((col) => (
                <th key={col} scope="col" className="log-label px-3 py-2 font-semibold">{col}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {artifacts.map((a, i) => (
              <tr key={i} className="border-b border-py-border transition-colors last:border-b-0 hover:bg-py-elevated">
                <td className="px-3 py-1.5 text-py-soft">{a.artifact_type}</td>
                <td className="num max-w-[420px] break-all px-3 py-1.5 text-xs text-py-text" title={a.path}>{a.path}</td>
                <td className="num px-3 py-1.5 text-xs text-py-muted">{a.size_bytes != null ? formatBytes(a.size_bytes) : '-'}</td>
                <td className="num px-3 py-1.5 text-xs text-py-muted">{a.step ?? '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export function NotesTab({ run, isAdmin, onChanged }: { run: Run; isAdmin: boolean; onChanged: () => void }) {
  const hasParams = Object.keys(run.hyperparams).length > 0
  return (
    <div className="flex flex-col gap-6">
      <section>
        <Heading>Notes</Heading>
        <RunNotes run={run} isAdmin={isAdmin} onChanged={onChanged} />
      </section>
      <section>
        <Heading>Hyperparameters</Heading>
        {hasParams ? (
          <HyperparamTable hyperparams={run.hyperparams} />
        ) : (
          <Note>No hyperparameters were recorded. Pass them to <span className="num">pyrometer.init(name, hyperparams={'{...}'})</span>.</Note>
        )}
      </section>
    </div>
  )
}

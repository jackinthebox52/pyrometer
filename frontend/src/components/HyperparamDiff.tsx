import type { Run } from '../api/types.ts'

interface Props {
  runs: Run[]
  colors: string[]
}

export default function HyperparamDiff({ runs, colors }: Props) {
  const allKeys = Array.from(
    new Set(runs.flatMap((r) => Object.keys(r.hyperparams))),
  ).sort()

  if (allKeys.length === 0) {
    return (
      <div className="rounded border border-py-border bg-py-surface py-8 text-center text-[13px] text-py-muted">
        No hyperparameters logged
      </div>
    )
  }

  return (
    <div className="overflow-x-auto rounded border border-py-border bg-py-surface">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="border-b border-py-border">
            <th className="min-w-[160px] px-4 py-2.5 text-left text-xs font-medium text-py-muted">Parameter</th>
            {runs.map((r, i) => (
              <th
                key={r.run_id}
                className="min-w-[140px] px-4 py-2.5 text-left text-xs font-medium"
                style={{ color: colors[i] }}
              >
                {r.name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {allKeys.map((key) => {
            const values = runs.map((r) => r.hyperparams[key])
            const serialized = values.map((v) => JSON.stringify(v))
            const isDiff = new Set(serialized).size > 1

            return (
              <tr
                key={key}
                className={`border-b border-py-border last:border-b-0 hover:bg-py-elevated ${isDiff ? 'bg-py-elevated' : ''}`}
              >
                <td className="whitespace-nowrap px-4 py-2 font-mono text-py-muted">
                  {key}
                  {isDiff && <span className="ml-2 text-[11px] font-sans text-py-soft">differs</span>}
                </td>
                {values.map((v, i) => (
                  <td
                    key={i}
                    className={`px-4 py-2 font-mono ${isDiff ? 'font-medium' : 'text-py-text'}`}
                    style={isDiff ? { color: colors[i] } : undefined}
                  >
                    {v === undefined ? <span className="text-py-faint">—</span> : JSON.stringify(v)}
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

interface Props {
  hyperparams: Record<string, unknown>
}

export default function HyperparamTable({ hyperparams }: Props) {
  const entries = Object.entries(hyperparams)
  if (entries.length === 0) return null

  return (
    <div className="overflow-hidden rounded border border-py-border bg-py-surface">
      <table className="w-full">
        <thead>
          <tr className="border-b border-py-border">
            <th className="w-2/5 px-4 py-2 text-left text-xs font-medium text-py-muted">Parameter</th>
            <th className="px-4 py-2 text-left text-xs font-medium text-py-muted">Value</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([k, v]) => (
            <tr key={k} className="border-b border-py-border last:border-b-0 hover:bg-py-elevated">
              <td className="px-4 py-2 font-mono text-xs text-py-muted">{k}</td>
              <td className="px-4 py-2 font-mono text-xs text-py-text">{JSON.stringify(v)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

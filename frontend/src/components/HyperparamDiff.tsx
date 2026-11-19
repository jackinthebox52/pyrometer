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
      <div
        className="text-xs font-mono text-center py-8 rounded-lg"
        style={{ color: '#4a4e60', backgroundColor: '#111318', border: '1px solid #252830' }}
      >
        No hyperparameters logged
      </div>
    )
  }

  return (
    <div className="overflow-x-auto rounded-lg" style={{ border: '1px solid #252830' }}>
      <table className="w-full text-xs font-mono border-collapse">
        <thead>
          <tr style={{ backgroundColor: '#0d0f13', borderBottom: '1px solid #252830' }}>
            <th
              className="text-left px-4 py-2.5 font-normal"
              style={{ color: '#4a4e60', minWidth: 160 }}
            >
              Parameter
            </th>
            {runs.map((r, i) => (
              <th
                key={r.run_id}
                className="text-left px-4 py-2.5 font-normal"
                style={{ color: colors[i], minWidth: 140 }}
              >
                {r.name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {allKeys.map((key, rowIdx) => {
            const values = runs.map((r) => r.hyperparams[key])
            const serialized = values.map((v) => JSON.stringify(v))
            const isDiff = new Set(serialized).size > 1

            return (
              <tr
                key={key}
                style={{
                  backgroundColor: isDiff
                    ? 'rgba(245, 158, 11, 0.05)'
                    : rowIdx % 2 === 0
                      ? '#0d0f13'
                      : '#111318',
                  borderTop: rowIdx > 0 ? '1px solid #1a1d24' : undefined,
                }}
              >
                <td
                  className="px-4 py-2"
                  style={{ color: '#8b8fa8', whiteSpace: 'nowrap' }}
                >
                  {isDiff && (
                    <span style={{ color: '#f59e0b', marginRight: 6 }}>◆</span>
                  )}
                  {key}
                </td>
                {values.map((v, i) => (
                  <td
                    key={i}
                    className="px-4 py-2"
                    style={{
                      color: isDiff ? colors[i] : '#e8eaf0',
                      fontWeight: isDiff ? 500 : 400,
                    }}
                  >
                    {v === undefined ? (
                      <span style={{ color: '#4a4e60' }}>—</span>
                    ) : (
                      JSON.stringify(v)
                    )}
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

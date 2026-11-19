interface Props {
  hyperparams: Record<string, unknown>
}

export default function HyperparamTable({ hyperparams }: Props) {
  const entries = Object.entries(hyperparams)
  if (entries.length === 0) return null

  return (
    <div className="rounded-lg overflow-hidden" style={{ border: '1px solid #252830' }}>
      <table className="w-full">
        <thead>
          <tr style={{ backgroundColor: '#1a1d24', borderBottom: '1px solid #252830' }}>
            <th
              className="px-4 py-2 text-left text-xs font-medium"
              style={{ color: '#4a4e60', width: '40%' }}
            >
              parameter
            </th>
            <th className="px-4 py-2 text-left text-xs font-medium" style={{ color: '#4a4e60' }}>
              value
            </th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([k, v], i) => (
            <tr
              key={k}
              style={{
                backgroundColor: i % 2 === 0 ? '#111318' : '#0e0f13',
                borderBottom: i < entries.length - 1 ? '1px solid #1a1d24' : 'none',
              }}
            >
              <td
                className="px-4 py-2 text-xs font-mono"
                style={{ color: '#8b8fa8' }}
              >
                {k}
              </td>
              <td
                className="px-4 py-2 text-xs font-mono"
                style={{ color: '#e8eaf0' }}
              >
                {JSON.stringify(v)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

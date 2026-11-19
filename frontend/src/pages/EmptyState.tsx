export default function EmptyState() {
  return (
    <div
      className="flex flex-col items-center justify-center h-full gap-3"
      style={{ color: '#4a4e60' }}
    >
      <span style={{ fontSize: 40, color: '#1e2128', lineHeight: 1 }}>◉</span>
      <p className="text-sm font-mono">Select a run from the sidebar</p>
      <p className="text-xs" style={{ color: '#353840' }}>
        or start a training run with{' '}
        <code className="font-mono" style={{ color: '#8b8fa8' }}>
          pyrometer.init()
        </code>
      </p>
    </div>
  )
}

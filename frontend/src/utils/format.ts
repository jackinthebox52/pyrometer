export function timeAgo(isoString: string): string {
  const diffMs = Date.now() - new Date(isoString).getTime()
  const secs = Math.floor(diffMs / 1000)
  if (secs < 60) return `${secs}s ago`
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

export function duration(start: string, end: string | null): string {
  if (!end) return '—'
  const secs = Math.floor((new Date(end).getTime() - new Date(start).getTime()) / 1000)
  if (secs < 60) return `${secs}s`
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ${secs % 60}s`
  const hours = Math.floor(mins / 60)
  return `${hours}h ${mins % 60}m`
}

export function formatMetricValue(v: number): string {
  const abs = Math.abs(v)
  if (abs === 0) return '0'
  if (abs >= 10000) return v.toExponential(2)
  if (abs >= 100) return v.toFixed(1)
  if (abs >= 1) return v.toFixed(3)
  if (abs >= 0.0001) return v.toFixed(4)
  return v.toExponential(2)
}

export function formatMicroseconds(us: number): string {
  if (us >= 1_000_000) return `${(us / 1_000_000).toFixed(2)}s`
  if (us >= 1_000) return `${(us / 1_000).toFixed(1)}ms`
  return `${us.toFixed(1)}μs`
}

export function formatBytesSigned(bytes: number): string {
  const abs = Math.abs(bytes)
  const sign = bytes < 0 ? '-' : '+'
  if (abs >= 1024 ** 3) return `${sign}${(abs / 1024 ** 3).toFixed(2)}GB`
  if (abs >= 1024 ** 2) return `${sign}${(abs / 1024 ** 2).toFixed(1)}MB`
  if (abs >= 1024) return `${sign}${(abs / 1024).toFixed(1)}KB`
  return `${sign}${abs}B`
}

export function formatBytes(bytes: number): string {
  const gb = bytes / 1024 ** 3
  if (gb >= 1) return `${gb.toFixed(1)} GB`
  return `${(bytes / 1024 ** 2).toFixed(0)} MB`
}

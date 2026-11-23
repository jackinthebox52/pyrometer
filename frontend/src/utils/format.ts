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

/** Y-axis gutter in px: fits the widest tick label (e.g. `-0.8929`, `-48.7k`) in the 10px mono axis font. */
export const Y_AXIS_WIDTH = 52

const SUFFIXES: Array<[number, string]> = [
  [1e12, 'T'],
  [1e9, 'B'],
  [1e6, 'M'],
  [1e3, 'k'],
]

/**
 * The one number formatter for charts (ticks, header values, legends, tooltips): at most 4 significant digits with
 * trailing zeros trimmed (12, 2.17, 0.8929, 93.81); k/M/B/T suffixes from 1e4 (48.7k, 1.05M); compact scientific below
 * 1e-3 (7.1e-5, 3e-4); "0" for zero and an em dash for non-finite input.
 */
export function formatNum(v: number): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  if (v === 0) return '0'
  const abs = Math.abs(v)
  if (abs < 1e-3) {
    const [m, e] = v.toExponential(1).split('e')
    return `${m.replace(/\.0$/, '')}e${e.replace('+', '')}`
  }
  const rounded = +v.toPrecision(4)
  if (Math.abs(rounded) < 1e4) return String(rounded)
  for (let i = 0; i < SUFFIXES.length; i++) {
    const [unit, suffix] = SUFFIXES[i]
    if (abs < unit) continue
    let scaled = +(v / unit).toPrecision(3)
    // 999.6k rounds to 1000k: promote to the next unit instead.
    if (Math.abs(scaled) >= 1000 && i > 0) {
      const [u2, s2] = SUFFIXES[i - 1]
      scaled = +(v / u2).toPrecision(3)
      return `${scaled}${s2}`
    }
    return `${scaled}${suffix}`
  }
  return String(rounded)
}

/** Alias kept for existing call sites: chart values (see `formatNum`). */
export const formatMetricValue = formatNum

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

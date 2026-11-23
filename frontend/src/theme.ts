/**
 * Design tokens for JS consumers (Recharts/d3 props, canvas, inline styles that need a computed colour). The same values
 * are CSS custom properties in `index.css` and Tailwind colours (`py-*`) in `tailwind.config.js`; keep the three in sync.
 *
 * World: a kiln firing log. Neutrals are one warm stepped ramp (no ad-hoc greys). The blackbody ramp carries heat and
 * data only, never chrome. Steel means finished; red means failed/crashed.
 */

/** Warm neutral ramp, darkest to lightest. Every neutral in the UI is one of these. */
export const N = {
  n0: '#110e0b', // sidebar / sunken
  n1: '#17130f', // page ground
  n2: '#1e1814', // panel
  n3: '#261f19', // raised, hover, input
  n4: '#30271f', // hairline (soft)
  n5: '#3f3329', // hairline (strong), control border
  n6: '#6b5b4d', // disabled text, axis ticks
  n7: '#a8998a', // muted text
  n8: '#d6c8b8', // secondary text
  n9: '#f3e9dc', // primary text
} as const

/** Blackbody incandescence, coolest (dull red) to hottest (white). Data and heat only. */
export const HEAT = ['#3b0f0a', '#7a2414', '#b8361a', '#e0521d', '#f6b23c', '#fbd36b', '#fff1cf'] as const

export const STEEL = '#8a97a6'
export const RED = '#e5484d'
export const GREEN = '#5fb87a'

/** Primary series colour for single-run charts (a hot, legible ramp step). */
export const SERIES = '#f6b23c'

export const C = {
  ...N,
  bg: N.n1,
  panel: N.n2,
  raised: N.n3,
  line: N.n4,
  lineHi: N.n5,
  faint: N.n6,
  muted: N.n7,
  soft: N.n8,
  text: N.n9,
  heat: HEAT[4],
  ember: HEAT[3],
  steel: STEEL,
  red: RED,
  green: GREEN,
  series: SERIES,
} as const

/** Status colours. Running glows (heat); finished states cool to steel; failures are red. */
export const STATUS_COLOR: Record<string, string> = {
  running: HEAT[4],
  completed: STEEL,
  failed: RED,
  crashed: RED,
  killed: N.n7,
}

function hexToRgb(h: string): [number, number, number] {
  const n = parseInt(h.slice(1), 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

/**
 * Colour on the blackbody ramp for t in [0, 1] (0 = cold dull red, 1 = white hot), linearly interpolated between ramp
 * steps. Non-finite t is treated as 0.
 */
export function heatColor(t: number): string {
  const x = Number.isFinite(t) ? Math.min(1, Math.max(0, t)) : 0
  const pos = x * (HEAT.length - 1)
  const i = Math.min(HEAT.length - 2, Math.floor(pos))
  const f = pos - i
  const a = hexToRgb(HEAT[i])
  const b = hexToRgb(HEAT[i + 1])
  const c = a.map((v, k) => Math.round(v + (b[k] - v) * f))
  return `#${c.map((v) => v.toString(16).padStart(2, '0')).join('')}`
}

/** CSS gradient across the whole blackbody ramp, left (cold) to right (hot). */
export const HEAT_GRADIENT = `linear-gradient(90deg, ${HEAT.join(', ')})`

/** Shared Recharts styling so every chart has the same axes, grid and tooltip. */
export const CHART = {
  grid: N.n4,
  axis: N.n5,
  tick: { fill: N.n7, fontSize: 10, fontFamily: '"JetBrains Mono", monospace' },
  tooltip: {
    contentStyle: {
      backgroundColor: N.n2,
      border: `1px solid ${N.n5}`,
      borderRadius: 2,
      fontSize: 11,
      fontFamily: '"JetBrains Mono", monospace',
      color: N.n9,
    },
    labelStyle: { color: N.n7 },
    itemStyle: { color: N.n9 },
  },
} as const

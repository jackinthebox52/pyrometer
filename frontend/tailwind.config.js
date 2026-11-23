/** @type {import('tailwindcss').Config} */
// Colours mirror src/theme.ts and the CSS variables in src/index.css.
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Archivo Variable"', 'Archivo', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      borderRadius: {
        DEFAULT: '2px',
        sm: '1px',
        md: '2px',
        lg: '2px',
        xl: '2px',
      },
      colors: {
        // Warm neutral ramp
        'py-n0': 'var(--n0)',
        'py-n1': 'var(--n1)',
        'py-n2': 'var(--n2)',
        'py-n3': 'var(--n3)',
        'py-n4': 'var(--n4)',
        'py-n5': 'var(--n5)',
        'py-n6': 'var(--n6)',
        'py-n7': 'var(--n7)',
        'py-n8': 'var(--n8)',
        'py-n9': 'var(--n9)',
        'py-base': 'var(--n1)',
        'py-surface': 'var(--n2)',
        'py-elevated': 'var(--n3)',
        'py-border': 'var(--n4)',
        'py-border-hi': 'var(--n5)',
        'py-faint': 'var(--n6)',
        'py-muted': 'var(--n7)',
        'py-soft': 'var(--n8)',
        'py-text': 'var(--n9)',
        // Blackbody ramp (data and heat only)
        'py-heat-0': 'var(--heat-0)',
        'py-heat-1': 'var(--heat-1)',
        'py-heat-2': 'var(--heat-2)',
        'py-heat-3': 'var(--heat-3)',
        'py-heat-4': 'var(--heat-4)',
        'py-heat-5': 'var(--heat-5)',
        'py-heat-6': 'var(--heat-6)',
        // States
        'py-amber': 'var(--heat-4)',
        'py-running': 'var(--heat-4)',
        'py-completed': 'var(--steel)',
        'py-steel': 'var(--steel)',
        'py-crashed': 'var(--red)',
        'py-red': 'var(--red)',
        'py-green': 'var(--green)',
        'py-killed': 'var(--n7)',
      },
    },
  },
  plugins: [],
}

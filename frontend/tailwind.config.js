/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Sora', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      colors: {
        'py-base': '#0a0b0e',
        'py-surface': '#111318',
        'py-elevated': '#1a1d24',
        'py-border': '#252830',
        'py-border-hi': '#353840',
        'py-text': '#e8eaf0',
        'py-muted': '#8b8fa8',
        'py-faint': '#4a4e60',
        'py-amber': '#f59e0b',
        'py-running': '#22c55e',
        'py-completed': '#60a5fa',
        'py-crashed': '#ef4444',
        'py-killed': '#eab308',
      },
    },
  },
  plugins: [],
}

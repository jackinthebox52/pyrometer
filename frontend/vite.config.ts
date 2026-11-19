import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/runs': { target: 'http://localhost:8321', changeOrigin: true },
      '/health': { target: 'http://localhost:8321', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8321', changeOrigin: true, ws: true },
    },
  },
})

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  plugins: [react()],
  // Two pages: the shopper's (index) and the family's order list (caregiver)
  build: { rolldownOptions: { input: ['index.html', 'caregiver.html'].map((f) => fileURLToPath(new URL(f, import.meta.url))) } },
  // Not Vite's defaults, so this runs alongside leash-pay (5173/8000)
  server: { port: 5174, strictPort: true, proxy: { '/api': 'http://localhost:8001' } },
})

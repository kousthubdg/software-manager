import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5179,
    proxy: { '/api': { target: process.env.SM_DEV_API || 'http://127.0.0.1:12999', changeOrigin: true, secure: false } },
  },
  build: { outDir: 'dist', chunkSizeWarningLimit: 1500 },
})

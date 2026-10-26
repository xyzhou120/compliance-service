import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/debug/',
  build: {
    outDir: '../compliance_service/web',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: { '/v1': 'http://127.0.0.1:8091' },
  },
})

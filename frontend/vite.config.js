import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
// Development-only native MoviePilot rendering harness. No remoteEntry in production.
export default defineConfig({ base: './', plugins: [vue()],
  build: { outDir: '../.work/preview', emptyOutDir: true } })

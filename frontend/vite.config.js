import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'

export default defineConfig(({ mode }) => ({
  base: mode === 'preview' ? './' : '/',
  plugins: [vue(), ...(mode === 'preview' ? [] : [federation({
    name: 'MediaArchiver', filename: 'remoteEntry.js',
    exposes: { './Page': './src/Page.vue', './Config': './src/Config.vue' },
    shared: { vue: { requiredVersion: false, generate: false } },
  })])],
  build: {
    target: 'esnext',
    outDir: mode === 'preview' ? '../../.work/preview' : '../plugins.v2/mediaarchiver/dist',
    emptyOutDir: true,
    rollupOptions: { input: mode === 'preview' ? 'index.html' : {},
      output: { chunkFileNames: 'assets/[name]-[hash].js', assetFileNames: 'assets/[name]-[hash][extname]' } },
  },
}))

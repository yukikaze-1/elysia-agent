import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/ws': {
        target: 'ws://localhost:8765',
        ws: true,
      },
      '/api': {
        target: 'http://localhost:8765',
      },
      '/health': {
        target: 'http://localhost:8765',
      },
      '/ready': {
        target: 'http://localhost:8765',
      },
      '/shutdown': {
        target: 'http://localhost:8765',
      },
      '/restart': {
        target: 'http://localhost:8765',
      },
      '/tools': {
        target: 'http://localhost:8765',
      },
      '/uploads': {
        target: 'http://localhost:8765',
      },
      '/live2d': {
        target: 'http://localhost:8765',
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        debug: resolve(__dirname, 'debug.html'),
      },
    },
  },
})

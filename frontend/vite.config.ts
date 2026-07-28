import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  base: '/static/',
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/ws': {
        target: 'http://localhost:8765',
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
      '/debug': {
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
    outDir: resolve(__dirname, '../static'),
    emptyOutDir: false,  // 不清空——保留 Elysia.png、live2d-demo/ 等非代码资源
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        debug: resolve(__dirname, 'debug.html'),
      },
    },
  },
})

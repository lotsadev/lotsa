import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'path'

// Test-only config kept separate from vite.config.ts so the build pipeline
// stays untouched. Mirrors the `@` alias and React plugin the app uses, and
// keeps the jsdom env + jest-dom matchers (registered in src/test/setup.ts)
// out of the production bundle. `include` globs the whole src tree, so every
// *.test.{ts,tsx} runs — UI-primitive tests and component tests alike.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})

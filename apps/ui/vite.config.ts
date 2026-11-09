import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * FastAPI serves `apps/ui/dist` as StaticFiles in production, so `base` stays '/'
 * and the build output directory is left at the default `dist`.
 *
 * In dev the UI runs on 127.0.0.1:5173 and proxies `/api` to the FastAPI process on
 * 127.0.0.1:8000. The proxy is what keeps the fetch URLs in `src/api/client.ts`
 * identical in dev and in production ('/api/...', never an absolute origin) — so
 * nothing has to know whether it is being served by Vite or by FastAPI.
 *
 * `host: '127.0.0.1'` (not 'localhost') is deliberate: on this machine 'localhost'
 * can resolve to ::1 first, and a FastAPI bound to 127.0.0.1 would then be missed
 * by the proxy with an opaque ECONNREFUSED.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 5173,
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});

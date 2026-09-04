import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/** Where `python3 backend.py` binds by default (API_HOST / API_PORT). */
const BACKEND_ORIGIN = 'http://127.0.0.1:8010'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // 5173 is one of the four origins the backend's CORS allow-list already
    // names, so a direct call would work too. The proxy is still the better
    // default: it makes every request same-origin in development, so the app
    // is never the thing that discovers a CORS misconfiguration, and the
    // client can use relative `/api/...` paths that stay correct in production
    // behind a single reverse proxy.
    proxy: {
      '/api': {
        target: BACKEND_ORIGIN,
        changeOrigin: true,
        // POST /api/investigate runs the whole LangGraph pipeline and can hold
        // the connection open for minutes; the backend's own deadline is
        // API_GRAPH_TIMEOUT (900s by default). A proxy timeout shorter than
        // that would fail the request in the browser while the analysis was
        // still running and would later be stored perfectly well.
        timeout: 15 * 60 * 1000,
        proxyTimeout: 15 * 60 * 1000,
      },
    },
  },
})

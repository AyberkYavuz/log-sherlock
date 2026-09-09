import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'

/**
 * Where the environment files live: the repository root, one level above this
 * one.
 *
 * This is the load-bearing line of the file. Vite reads `.env` from its own
 * root — `frontend/` — by default, so the repository-root `.env` that every
 * Python entry point reads was previously invisible to the client, and
 * `VITE_API_BASE_URL` could only have been set by a second env file nobody
 * had. Pointing `envDir` here is what makes one file configure both halves of
 * the stack.
 */
const ENV_DIR = fileURLToPath(new URL('..', import.meta.url))

/** Where `python3 backend.py` binds when nothing is configured. */
const DEFAULT_BACKEND_ORIGIN = 'http://127.0.0.1:8010'

/**
 * How long the dev proxy waits on one request.
 *
 * `POST /api/investigate` runs the whole LangGraph pipeline and can hold the
 * connection open for minutes; the backend's own deadline is
 * `API_GRAPH_TIMEOUT`, 900 seconds by default. A proxy timeout shorter than
 * that would fail the request in the browser while an analysis that goes on to
 * store perfectly well was still running.
 */
const PROXY_TIMEOUT_MS = 15 * 60 * 1000

/**
 * Addresses that mean "every interface" when a server binds them, and mean
 * nothing useful when a client dials them.
 *
 * `API_HOST=0.0.0.0` is correct for the backend in a container and wrong as a
 * proxy target, so it is translated rather than forwarded.
 */
const WILDCARD_HOSTS = new Set(['0.0.0.0', '::', '[::]', '*'])

/**
 * The origin the dev proxy forwards `/api` to.
 *
 * Read from the environment rather than hardcoded, so a backend moved with
 * `API_PORT` does not need a second edit here. Three sources, in descending
 * order of specificity:
 *
 *   1. `VITE_API_BASE_URL`, when it is absolute — the client's own target is
 *      the most direct statement of where the API is. Only its origin is used;
 *      the `/api` path is the proxy rule's job.
 *   2. `API_HOST` / `API_PORT` — the same two variables the backend binds
 *      with, so the two cannot disagree.
 *   3. The backend's own default.
 *
 * A relative `VITE_API_BASE_URL` (`/api`) is skipped rather than treated as a
 * failure: it is the value that *asks* for this proxy, so it cannot also
 * describe its target.
 */
function resolveBackendOrigin(env: Record<string, string>): string {
  const configured = (env.VITE_API_BASE_URL ?? '').trim()
  if (configured && !configured.startsWith('/')) {
    try {
      return new URL(configured).origin
    } catch {
      // Absolute-looking but unparseable. Fall through to API_HOST/API_PORT
      // rather than crashing the dev server over one malformed value.
    }
  }

  const host = (env.API_HOST ?? '').trim()
  const port = (env.API_PORT ?? '').trim()
  if (host || port) {
    const resolvedHost = !host || WILDCARD_HOSTS.has(host) ? '127.0.0.1' : host
    const resolvedPort = port || '8010'
    return `http://${resolvedHost}:${resolvedPort}`
  }

  return DEFAULT_BACKEND_ORIGIN
}

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // An empty prefix loads every key in the file, not just the `VITE_` ones,
  // because the proxy target above is derived from `API_HOST` / `API_PORT`.
  // That is safe: what reaches the browser is governed by `envPrefix` below,
  // not by what this config reads. The provider keys sitting in the same file
  // are visible to the Node process that builds the bundle and to nothing
  // inside it.
  const env = loadEnv(mode, ENV_DIR, '')
  const target = resolveBackendOrigin(env)

  return {
    plugins: [react()],
    envDir: ENV_DIR,
    // Explicit rather than left to the default, because `envDir` now points at
    // a file holding OPENAI_API_KEY, ANTHROPIC_API_KEY, TAVILY_API_KEY and the
    // database password. This one line is what keeps every one of them out of
    // `dist/assets/*.js`.
    envPrefix: 'VITE_',
    server: {
      // 5173 is one of the four origins the backend's CORS allow-list already
      // names, so a direct call works too — and does happen, because `.env`
      // now ships an absolute `VITE_API_BASE_URL`. The proxy stays configured
      // for the relative-path case: blank that variable and every request
      // becomes same-origin in development, which is also what makes one
      // reverse proxy work in production.
      proxy: {
        '/api': {
          target,
          changeOrigin: true,
          timeout: PROXY_TIMEOUT_MS,
          proxyTimeout: PROXY_TIMEOUT_MS,
        },
      },
    },
  }
})

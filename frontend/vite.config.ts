import react from '@vitejs/plugin-react'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
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
 * The environment-file selection rule, mirrored from
 * `graph_library/env_files.py`.
 *
 * It has to be a mirror rather than a shared implementation — one side is
 * Python and the other is the TypeScript that configures the bundler — so the
 * two are kept deliberately short and each names the other. If the rule below
 * changes, that module changes with it.
 *
 * `ENV_FILE` first, then `.env.docker` under a container indicator, then
 * `.env`. Vite's own `loadEnv` reads `.env`, `.env.local` and `.env.[mode]` by
 * convention and knows nothing about `.env.docker`, so anything other than the
 * default has to be read here explicitly — otherwise this config could log
 * that it resolved `.env.docker` while the bundle still carried `.env`'s
 * values, which is a worse failure than not supporting the file at all.
 */
const ENV_FILE_VAR = 'ENV_FILE'
const LOCAL_ENV_FILE = '.env'
const DOCKER_ENV_FILE = '.env.docker'
const CONTAINER_ENV_VARS = ['DOCKER_CONTAINER', 'KUBERNETES_SERVICE_HOST', 'container']
const CONTAINER_MARKER_PATH = '/.dockerenv'

/** Prefix on every line this config logs, matching the Python services'. */
const LOG_PREFIX = '[Config]'

/** Why this process looks containerized, or `null` if it does not. */
function containerIndicator(): string | null {
  for (const name of CONTAINER_ENV_VARS) {
    if ((process.env[name] ?? '').trim()) return `${name} is set`
  }
  try {
    if (existsSync(CONTAINER_MARKER_PATH)) return `${CONTAINER_MARKER_PATH} exists`
  } catch {
    return null
  }
  return null
}

/**
 * A minimal `KEY=VALUE` reader for a file Vite will not read itself.
 *
 * Hand-written rather than pulled from `dotenv`: that package is a transitive
 * dependency of Vite rather than a declared one here, and adding a direct
 * dependency to parse the handful of `VITE_` keys in one file is a poor trade.
 * It handles what these files actually contain — comments, blank lines,
 * `export` prefixes, and single or double quotes — and nothing more exotic.
 */
function parseEnvFile(path: string): Record<string, string> {
  const parsed: Record<string, string> = {}
  let text: string
  try {
    text = readFileSync(path, 'utf8')
  } catch {
    return parsed
  }

  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#')) continue
    const match = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$/.exec(line)
    if (!match) continue
    let value = match[2].trim()
    if (
      (value.startsWith('"') && value.endsWith('"') && value.length > 1) ||
      (value.startsWith("'") && value.endsWith("'") && value.length > 1)
    ) {
      value = value.slice(1, -1)
    }
    parsed[match[1]] = value
  }
  return parsed
}

interface EnvFileResolution {
  /** The file's name as asked for, or `''` when none was in play. */
  name: string
  /** The absolute path, or `null` when nothing was found. */
  path: string | null
  /** Which rule chose it: `ENV_FILE`, `container`, `default` or `none`. */
  selectedBy: string
  /** Whether Vite's own `loadEnv` already covers this file. */
  readByVite: boolean
  /** The one-line sentence to log. */
  detail: string
}

/**
 * Decide which environment file this build or dev server is configured by.
 *
 * The `readByVite` flag is the interesting part. `.env` is Vite's own
 * convention, so resolving it means the work is already done. Anything else has
 * to be parsed here and injected, and saying which of the two happened is what
 * makes the log line honest.
 */
function resolveEnvFile(): EnvFileResolution {
  const indicator = containerIndicator()

  const explicit = (process.env[ENV_FILE_VAR] ?? '').trim()
  if (explicit) {
    const path = join(ENV_DIR, explicit)
    if (!existsSync(path)) {
      return {
        name: explicit,
        path: null,
        selectedBy: ENV_FILE_VAR,
        readByVite: false,
        detail:
          `${ENV_FILE_VAR}='${explicit}' was requested but no such file exists ` +
          `at ${path}. Nothing was loaded from a file.`,
      }
    }
    return {
      name: explicit,
      path,
      selectedBy: ENV_FILE_VAR,
      readByVite: explicit === LOCAL_ENV_FILE,
      detail: '',
    }
  }

  if (indicator) {
    const dockerPath = join(ENV_DIR, DOCKER_ENV_FILE)
    if (existsSync(dockerPath)) {
      return {
        name: DOCKER_ENV_FILE,
        path: dockerPath,
        selectedBy: 'container',
        readByVite: false,
        detail: '',
      }
    }
  }

  const localPath = join(ENV_DIR, LOCAL_ENV_FILE)
  if (existsSync(localPath)) {
    return {
      name: LOCAL_ENV_FILE,
      path: localPath,
      selectedBy: 'default',
      readByVite: true,
      detail: '',
    }
  }

  return {
    name: '',
    path: null,
    selectedBy: 'none',
    readByVite: false,
    detail:
      `No environment file found in ${ENV_DIR}. Falling back to the built-in ` +
      'defaults; VITE_API_BASE_URL is unset, so the client will use the ' +
      'relative /api path.',
  }
}

/** Why this file, phrased for the end of a sentence. */
function selectionReason(resolution: EnvFileResolution): string {
  if (resolution.selectedBy === ENV_FILE_VAR) return `selected by ${ENV_FILE_VAR}`
  if (resolution.selectedBy === 'container') {
    return `selected because ${containerIndicator() ?? 'a container indicator'}`
  }
  if (containerIndicator()) {
    return (
      `selected by default — ${DOCKER_ENV_FILE} was not found even though ` +
      `${containerIndicator()}`
    )
  }
  return `selected by default — no ${ENV_FILE_VAR} override, not containerized`
}

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
export default defineConfig(({ mode, command }) => {
  // An empty prefix loads every key in the file, not just the `VITE_` ones,
  // because the proxy target above is derived from `API_HOST` / `API_PORT`.
  // That is safe: what reaches the browser is governed by `envPrefix` below,
  // not by what this config reads. The provider keys sitting in the same file
  // are visible to the Node process that builds the bundle and to nothing
  // inside it.
  const env = loadEnv(mode, ENV_DIR, '')

  // Which file this run is configured by, decided before anything is derived
  // from it. Logged unconditionally — `npm run dev` and `npm run build` both
  // say it — because "which env file" is the fact every value below depends on,
  // and the frontend is the half of the stack where getting it wrong is
  // invisible until a request 404s in a browser somewhere else.
  const resolution = resolveEnvFile()

  // A file Vite does not read by convention has to be read here, and its
  // `VITE_` keys injected, or the log line above would be a claim the bundle
  // does not honour. Only `VITE_`-prefixed keys are taken, which keeps this
  // path under the same rule as `envPrefix`: nothing else in the file can reach
  // the browser through it.
  const injected: Record<string, string> = {}
  if (resolution.path && !resolution.readByVite) {
    for (const [key, value] of Object.entries(parseEnvFile(resolution.path))) {
      if (key.startsWith('VITE_')) {
        env[key] = value
        injected[`import.meta.env.${key}`] = JSON.stringify(value)
      }
    }
  }

  if (resolution.detail) {
    console.warn(`${LOG_PREFIX} ${resolution.detail}`)
  } else {
    const keys = Object.keys(injected)
    const how = resolution.readByVite
      ? "read by Vite's own env loading"
      : `parsed by vite.config.ts and injected (${keys.length} VITE_ key${
          keys.length === 1 ? '' : 's'
        })`
    console.log(
      `${LOG_PREFIX} Sourced environment variables from ${resolution.name} ` +
        `(${selectionReason(resolution)}; ${how})`,
    )
  }

  const target = resolveBackendOrigin(env)
  console.log(
    `${LOG_PREFIX} ${command === 'serve' ? 'dev server' : 'build'}: ` +
      `VITE_API_BASE_URL=${env.VITE_API_BASE_URL || '(unset — client will use /api)'}` +
      `${command === 'serve' ? `, /api proxied to ${target}` : ''}`,
  )

  return {
    plugins: [react()],
    envDir: ENV_DIR,
    // Explicit rather than left to the default, because `envDir` now points at
    // a file holding OPENAI_API_KEY, ANTHROPIC_API_KEY, TAVILY_API_KEY and the
    // database password. This one line is what keeps every one of them out of
    // `dist/assets/*.js`.
    envPrefix: 'VITE_',
    // Empty on the default path, so Vite's own env handling is untouched. Only
    // populated when the resolved file is one Vite would not have read.
    define: injected,
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

/**
 * The one module that talks to the LogSherlock API.
 *
 * Everything HTTP lives here: the base path, the request bodies, and — most of
 * the point — the single place `{ "detail": ... }` is unwrapped into something
 * a component can render. Nothing above this layer sees a `Response`, a status
 * code or a raw envelope; callers get typed data or an `ApiError`.
 *
 * Three decisions shape the module:
 *
 *   * **One error type for every failure.** A 404, a 422, a 503, an unreachable
 *     server and a proxy that answered with HTML all arrive as `ApiError`, so a
 *     caller writes one `catch` rather than branching on how the failure was
 *     shaped. The status is kept on the error for the cases that genuinely
 *     differ — 503 means retry later, 500 means do not.
 *   * **The body is read as text first.** The backend answers JSON, but a dev
 *     proxy with no backend behind it answers a 502 with an HTML page, and
 *     `response.json()` would throw there and lose the status entirely.
 *   * **Aborts are not errors.** Every function takes an optional signal, and a
 *     cancelled request rethrows the `AbortError` untouched so a hook can tell
 *     "the component moved on" from "the request failed".
 */

import type {
  HealthResponse,
  InvestigateRequest,
  InvestigateResponse,
  InvestigationDetailResponse,
  PaginatedInvestigationsResponse,
} from '../types/api'

/**
 * Where the API lives. Relative by default, which is what makes the Vite proxy
 * work in development and a single reverse proxy work in production; override
 * with `VITE_API_BASE_URL` for a deployment that serves the two from different
 * origins.
 */
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api'

/** Pagination defaults, matching `backend/schemas.py`. */
export const DEFAULT_PAGE = 1
export const DEFAULT_LIMIT = 10

/**
 * A failure the API reported, or one that stopped a request reaching it.
 *
 * `status` is `0` for a transport failure — DNS, a refused connection, the
 * backend not running — because there was no HTTP response to take a code
 * from. Anything else is the real status.
 */
export class ApiError extends Error {
  /** HTTP status, or `0` when the request never got a response. */
  readonly status: number
  /** The raw `detail` value, kept so a caller can inspect a 422's issue list. */
  readonly detail: unknown

  constructor(message: string, status: number, detail: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    // Restores the prototype chain when this is downlevelled, so
    // `err instanceof ApiError` holds regardless of the compile target.
    Object.setPrototypeOf(this, ApiError.prototype)
  }

  /** Whether retrying later is reasonable: the dependency is down, not the request. */
  get isRetryable(): boolean {
    return this.status === 0 || this.status === 503 || this.status === 504
  }
}

/** Whether a thrown value is a cancellation rather than a failure. */
export function isAbortError(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === 'AbortError'
}

/**
 * Coerce anything thrown by this module into an `ApiError`.
 *
 * Exported because a hook's `catch` receives `unknown` and needs a typed value
 * for its state without repeating this narrowing four times.
 */
export function toApiError(cause: unknown): ApiError {
  if (cause instanceof ApiError) return cause
  if (cause instanceof Error) return new ApiError(cause.message, 0)
  return new ApiError(String(cause), 0)
}

/** One entry of Pydantic's 422 issue list, as much of it as is worth showing. */
interface ValidationIssue {
  loc?: unknown[]
  msg?: string
}

/** Render one validation issue as `body.raw_logs: Field required`. */
function formatIssue(issue: unknown): string | null {
  if (!issue || typeof issue !== 'object') return null
  const { loc, msg } = issue as ValidationIssue
  if (typeof msg !== 'string') return null
  const path = Array.isArray(loc) ? loc.join('.') : ''
  return path ? `${path}: ${msg}` : msg
}

/**
 * Turn a `detail` value into one sentence.
 *
 * The backend puts a string there for a deliberate failure and Pydantic's issue
 * list there for a 422, and a client that rendered the second with
 * `String(detail)` would show `[object Object]` for the single most common
 * failure a form produces.
 */
function describeDetail(detail: unknown, response: Response): string {
  if (typeof detail === 'string' && detail.trim()) return detail.trim()

  if (Array.isArray(detail)) {
    const issues = detail.map(formatIssue).filter((line): line is string => !!line)
    if (issues.length) return issues.join('; ')
  }

  if (detail && typeof detail === 'object') return JSON.stringify(detail)

  const status = `${response.status} ${response.statusText}`.trim()
  return status || 'The request failed.'
}

/** Read a response body as JSON where possible, as text otherwise. */
async function readBody(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    // A non-JSON body is itself the diagnosis — an HTML 502 from the dev proxy,
    // or a gateway page. Kept verbatim rather than discarded.
    return text
  }
}

/** Pull `detail` out of an error envelope, falling back to the whole body. */
function extractDetail(body: unknown): unknown {
  if (body && typeof body === 'object' && 'detail' in body) {
    return (body as { detail: unknown }).detail
  }
  return body
}

/**
 * Issue one request and return its parsed body.
 *
 * @throws ApiError on any status >= 400 and on any transport failure.
 * @throws DOMException (`AbortError`) when `signal` was aborted — deliberately
 *   not wrapped, so a caller can ignore its own cancellations.
 */
async function request<T>(
  path: string,
  init: RequestInit & { signal?: AbortSignal },
): Promise<T> {
  const url = `${API_BASE_URL}${path}`

  let response: Response
  try {
    response = await fetch(url, init)
  } catch (cause) {
    if (isAbortError(cause)) throw cause
    throw new ApiError(
      `Could not reach the LogSherlock API at ${url}. Check that the backend ` +
        'is running (python3 backend.py).',
      0,
      cause instanceof Error ? cause.message : String(cause),
    )
  }

  const body = await readBody(response)

  if (!response.ok) {
    const detail = extractDetail(body)
    throw new ApiError(describeDetail(detail, response), response.status, detail)
  }

  return body as T
}

/** A JSON POST, with the header and serialization done once. */
function postJson<T>(
  path: string,
  payload: unknown,
  signal?: AbortSignal,
): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
}

/**
 * `GET /api/health` — is the process up and serving?
 *
 * Touches no dependency, so a `200` here while the storage endpoints report
 * `503` says the database is what is unreachable, not the API.
 */
export function checkHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return request<HealthResponse>('/health', { method: 'GET', signal })
}

/**
 * `POST /api/investigate` — run the whole pipeline and store the result.
 *
 * This can hold the connection open for minutes: the payload size is the
 * caller's choice and the backend's own deadline is 900 seconds by default. A
 * resolved promise with `db_persisted: false` is a *success* carrying bad news
 * — the analysis ran and `investigation_notes` says why it was not stored.
 */
export function runInvestigation(
  payload: InvestigateRequest,
  signal?: AbortSignal,
): Promise<InvestigateResponse> {
  return postJson<InvestigateResponse>('/investigate', payload, signal)
}

/**
 * `POST /api/investigations` — one page of record metadata, newest first.
 *
 * A POST rather than a GET because the endpoint takes a JSON body, and a GET
 * with a body is handled inconsistently by proxies and `fetch`. The report is
 * excluded at the SQL projection, so this stays light however large the stored
 * documents are.
 *
 * @param page 1-based. A page past the end returns empty `items` with the real
 *   `total`, which is what lets a client recover from a stale pager.
 * @param limit Rows per page, 1-100. Above 100 is a 422.
 */
export function listInvestigations(
  page: number = DEFAULT_PAGE,
  limit: number = DEFAULT_LIMIT,
  signal?: AbortSignal,
): Promise<PaginatedInvestigationsResponse> {
  return postJson<PaginatedInvestigationsResponse>(
    '/investigations',
    { page, limit },
    signal,
  )
}

/**
 * `POST /api/investigations/{id}` — one investigation's full stored report.
 *
 * The body is empty today; `{}` is sent rather than nothing so that a future
 * filter is an added field rather than a changed call.
 *
 * @throws ApiError with `status: 404` when no such record exists.
 */
export function getInvestigationDetail(
  id: string,
  signal?: AbortSignal,
): Promise<InvestigationDetailResponse> {
  return postJson<InvestigationDetailResponse>(
    `/investigations/${encodeURIComponent(id)}`,
    {},
    signal,
  )
}

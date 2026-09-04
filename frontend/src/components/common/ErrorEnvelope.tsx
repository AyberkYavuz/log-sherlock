/**
 * How an `ApiError` is shown, everywhere it is shown.
 *
 * The backend answers every failure with one `{ "detail": ... }` envelope, so
 * the UI answers with one component — a form and a table reporting a 503 in two
 * different shapes would be two different bugs to notice.
 *
 * A retryable failure gets a retry affordance and an unretryable one does not,
 * because the distinction is real: a 503 means the database is down and trying
 * again later is exactly right, a 422 means the request itself is wrong.
 */

import type { ApiError } from '../../services/api'

export function ErrorEnvelope({
  error,
  onRetry,
}: {
  error: ApiError
  onRetry?: () => void
}) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-severity-error/40 bg-severity-error/10 p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="h-2 w-2 shrink-0 rounded-full bg-severity-error" />
        <span className="font-mono text-xs font-semibold uppercase tracking-wide text-severity-error">
          {error.status ? `HTTP ${error.status}` : 'Network error'}
        </span>
        {error.isRetryable && onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="ml-auto rounded border border-severity-error/40 px-2 py-1 text-xs font-medium text-severity-error transition-colors hover:bg-severity-error/20"
          >
            Try again
          </button>
        )}
      </div>
      <p className="mt-2 text-sm text-slate-200">{error.message}</p>
    </div>
  )
}

/**
 * The application's top bar: brand, and whether the backend is reachable.
 *
 * It owns the health check rather than receiving it, because the answer is
 * global and belongs to the chrome — no view below needs to know the backend is
 * up in order to render, and threading it down would make every one of them
 * pass a prop it does not read.
 *
 * The badge distinguishes three states, not two. "Checking" is its own state on
 * first paint: reporting "Backend Offline" before the first request has
 * answered would tell the user the system is down every single time the page
 * loads.
 */

import { useHealthCheck } from '../../hooks/useHealthCheck'

export function Header() {
  const { health, loading, error } = useHealthCheck()

  // Unknown until the first request settles. `loading` alone is not enough —
  // a later refetch should keep showing the last known state rather than
  // flicker back to "checking".
  const settled = health !== null || error !== null
  const online = health !== null && error === null

  const badge = !settled
    ? {
        label: 'Checking…',
        dot: 'bg-severity-info animate-pulse',
        text: 'text-severity-info',
        ring: 'border-severity-info/30 bg-severity-info/10',
      }
    : online
      ? {
          label: 'System Online',
          dot: 'bg-severity-success',
          text: 'text-severity-success',
          ring: 'border-severity-success/30 bg-severity-success/10',
        }
      : {
          label: 'Backend Offline',
          dot: 'bg-severity-error',
          text: 'text-severity-error',
          ring: 'border-severity-error/30 bg-severity-error/10',
        }

  return (
    <header className="sticky top-0 z-20 border-b border-obsidian-800 bg-obsidian-900">
      <div className="mx-auto flex max-w-7xl items-center gap-3 px-4 py-3 sm:px-6 lg:px-8">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-purple font-mono text-sm font-bold text-white">
          LS
        </span>
        <div className="leading-tight">
          <h1 className="text-base font-semibold tracking-tight text-slate-100">
            log-sherlock
          </h1>
          <p className="hidden text-xs text-severity-muted sm:block">
            Multi-agent log analysis
          </p>
        </div>

        <span
          className={`ml-auto flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium ${badge.ring}`}
          // Announced politely so a screen reader hears the backend going down
          // without having the current sentence interrupted.
          role="status"
          aria-live="polite"
          title={error ? error.message : undefined}
        >
          <span className={`h-2 w-2 shrink-0 rounded-full ${badge.dot}`} />
          <span className={badge.text}>{badge.label}</span>
          {settled && loading && (
            <span className="text-severity-muted" aria-hidden="true">
              ·
            </span>
          )}
        </span>
      </div>
    </header>
  )
}

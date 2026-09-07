/**
 * The stored-investigation list, one page at a time.
 *
 * Presentational: the caller owns `useInvestigations` and passes its state in,
 * because the page cursor is shared with the layout (an empty table decides
 * which scenario renders) and a component that owned it privately would keep
 * that decision to itself.
 *
 * Two details of the payload are load-bearing here. `confidence_score` is
 * nullable and `null` means *not measured*, which is a different fact from
 * `0` — the badge says so rather than rendering a zero. And `total_pages` is
 * `0` for an empty table rather than `1`, so the pager reads it directly
 * instead of special-casing one page containing nothing.
 *
 * Deletion is confirmed inline, in the row, rather than in a modal. The row is
 * where the record's identity already is — the id, the application, the score
 * and the date are all on screen and stay on screen — whereas a dialog has to
 * re-state which record it means and can only quote a fragment of it. It also
 * keeps the destructive click and its confirmation in one place instead of
 * moving the pointer to a floating box, and needs no focus trap or escape
 * handling to be accessible.
 */

import { useState } from 'react'

import { ErrorEnvelope } from '../common/ErrorEnvelope'
import { Spinner } from '../common/Spinner'
import type { ApiError } from '../../services/api'
import type {
  InvestigationItem,
  PaginatedInvestigationsResponse,
} from '../../types/api'

/** Score thresholds, highest first. */
const SCORE_TIERS: { min: number; className: string }[] = [
  { min: 80, className: 'bg-severity-success/10 text-severity-success ring-severity-success/30' },
  { min: 50, className: 'bg-severity-warn/10 text-severity-warn ring-severity-warn/30' },
  { min: 0, className: 'bg-severity-error/10 text-severity-error ring-severity-error/30' },
]

const UNMEASURED_TIER =
  'bg-severity-muted/10 text-severity-muted ring-severity-muted/30'

function ScoreBadge({ score }: { score: number | null | undefined }) {
  // `null` and `undefined` both mean the run recorded no score. Rendering that
  // as `0` would report a maximally uncertain investigation where there was
  // simply nothing to measure.
  if (score === null || score === undefined) {
    return (
      <span
        title="Not measured"
        className={`inline-flex rounded-md px-2 py-0.5 font-mono text-xs font-medium ring-1 ring-inset ${UNMEASURED_TIER}`}
      >
        n/a
      </span>
    )
  }

  const tier =
    SCORE_TIERS.find((candidate) => score >= candidate.min) ?? SCORE_TIERS[2]

  return (
    <span
      className={`inline-flex rounded-md px-2 py-0.5 font-mono text-xs font-medium ring-1 ring-inset ${tier.className}`}
    >
      {score}
    </span>
  )
}

/** Render an ISO-8601 timestamp in the reader's locale, keeping the original. */
function formatTimestamp(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function Cell({
  value,
  className = '',
}: {
  value: string | null | undefined
  className?: string
}) {
  return (
    <td className={`px-3 py-2.5 text-sm ${className}`}>
      {value ? (
        value
      ) : (
        // Every column but the id is nullable, because a run that degraded
        // before it recorded a provider still has a row worth listing.
        <span className="text-severity-muted">—</span>
      )}
    </td>
  )
}

function TrashIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-3.5 w-3.5"
      aria-hidden="true"
    >
      <path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14" />
      <path d="M10 11v5M14 11v5" />
    </svg>
  )
}

function Row({
  item,
  selected,
  onSelect,
  confirming,
  onRequestDelete,
  onCancelDelete,
  onConfirmDelete,
  deleting,
  deleteDisabled,
}: {
  item: InvestigationItem
  selected: boolean
  onSelect: (id: string) => void
  confirming: boolean
  onRequestDelete: (id: string) => void
  onCancelDelete: () => void
  onConfirmDelete: (id: string) => void
  deleting: boolean
  /** Another row is mid-delete, so this one's trash button is inert. */
  deleteDisabled: boolean
}) {
  return (
    <tr
      onClick={() => onSelect(item.investigation_id)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect(item.investigation_id)
        }
      }}
      tabIndex={0}
      aria-selected={selected}
      aria-busy={deleting}
      className={`cursor-pointer border-t border-obsidian-800 transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-brand-purple ${
        deleting ? 'opacity-50' : ''
      } ${
        confirming
          ? 'bg-severity-error/10 ring-1 ring-inset ring-severity-error/40'
          : selected
            ? 'bg-brand-purple/10 ring-1 ring-inset ring-brand-purple/40'
            : 'hover:bg-obsidian-800/50'
      }`}
    >
      <td className="px-3 py-2.5 font-mono text-xs text-slate-300">
        {item.investigation_id}
      </td>
      <Cell value={item.application_name} className="text-slate-200" />
      <Cell value={item.analysis_mode} className="capitalize text-slate-300" />
      <Cell value={item.llm_provider} className="text-slate-300" />
      <td className="px-3 py-2.5">
        <ScoreBadge score={item.confidence_score} />
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-sm text-severity-muted">
        <time dateTime={item.created_at} title={item.created_at}>
          {formatTimestamp(item.created_at)}
        </time>
      </td>
      {/* Every control here stops propagation: the row itself is a button that
          opens the inspection panel, so a click that reached it would select
          the record the user is trying to delete. */}
      <td
        className="whitespace-nowrap px-3 py-2.5 text-right"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => event.stopPropagation()}
      >
        {deleting ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-severity-muted">
            <Spinner className="h-3 w-3 text-severity-error" />
            Deleting…
          </span>
        ) : confirming ? (
          <span className="inline-flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => onConfirmDelete(item.investigation_id)}
              className="rounded border border-severity-error/50 bg-severity-error/15 px-2 py-1 text-xs font-semibold text-severity-error transition-colors hover:bg-severity-error/25"
            >
              Delete
            </button>
            <button
              type="button"
              onClick={onCancelDelete}
              // Autofocused so Escape-by-keyboard is not the only way out and
              // the safe choice is the one already under the cursor's focus.
              autoFocus
              className="rounded border border-obsidian-800 px-2 py-1 text-xs font-medium text-slate-300 transition-colors hover:text-white"
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            onClick={() => onRequestDelete(item.investigation_id)}
            disabled={deleteDisabled}
            aria-label={`Delete investigation ${item.investigation_id}`}
            title={`Delete ${item.investigation_id}`}
            className="inline-flex items-center rounded border border-transparent p-1.5 text-severity-muted transition-colors hover:border-severity-error/40 hover:bg-severity-error/10 hover:text-severity-error focus:outline-none focus-visible:ring-1 focus-visible:ring-severity-error disabled:cursor-not-allowed disabled:opacity-30"
          >
            <TrashIcon />
          </button>
        )}
      </td>
    </tr>
  )
}

export function InvestigationHistoryTable({
  data,
  loading,
  error,
  page,
  onPageChange,
  onRefresh,
  selectedId,
  onSelectRow,
  onDelete,
  deletingId = null,
  deleteError = null,
  onDismissDeleteError,
}: {
  data: PaginatedInvestigationsResponse | null
  loading: boolean
  error: ApiError | null
  page: number
  onPageChange: (page: number) => void
  onRefresh: () => void
  selectedId: string | null
  onSelectRow: (id: string) => void
  /** Delete one record. The caller owns the call and the refetch after it. */
  onDelete: (id: string) => void
  /** The id currently being deleted, or `null`. */
  deletingId?: string | null
  /** A failed delete, shown above the table rather than inside a row. */
  deleteError?: ApiError | null
  onDismissDeleteError?: () => void
}) {
  const items = data?.items ?? []
  const totalPages = data?.total_pages ?? 0
  const total = data?.total ?? 0

  // Which row is asking "are you sure?". One at a time by construction, since
  // it is a single id rather than a set: opening a second confirmation closes
  // the first, so there is never more than one armed destructive button.
  const [confirmingId, setConfirmingId] = useState<string | null>(null)

  return (
    <section className="rounded-xl border border-obsidian-800 bg-obsidian-900 shadow-lg shadow-black/20">
      <div className="flex flex-wrap items-center gap-3 border-b border-obsidian-800 px-5 py-4">
        <div>
          <h2 className="text-base font-semibold text-slate-100">History</h2>
          <p className="text-xs text-severity-muted">
            {total} stored {total === 1 ? 'investigation' : 'investigations'},
            newest first
          </p>
        </div>
        {loading && (
          <Spinner className="h-4 w-4 text-severity-info" />
        )}
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          className="ml-auto rounded-lg border border-obsidian-800 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors hover:border-brand-purple/50 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
        >
          Refresh
        </button>
      </div>

      {error ? (
        <div className="p-5">
          <ErrorEnvelope error={error} onRetry={onRefresh} />
        </div>
      ) : (
        <>
          {/* A failed delete is reported above the table, not in the row: the
              row may well be gone by the time the failure renders.

              The refresh control is rendered here rather than left to
              `ErrorEnvelope`'s own retry affordance, which appears only for
              statuses it considers retryable — 0, 503, 504. The most likely
              failure of a *delete* is a 404, meaning the record was already
              removed elsewhere, and that is the one case where the list on
              screen is provably stale and refreshing is exactly the right
              move. Deferring to the envelope would hide the recovery action
              precisely when it is most useful, and leave no way to dismiss the
              message. */}
          {deleteError && (
            <div className="space-y-2 px-5 pt-4">
              <ErrorEnvelope error={deleteError} />
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    onDismissDeleteError?.()
                    onRefresh()
                  }}
                  className="rounded-lg border border-obsidian-800 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors hover:border-brand-purple/50 hover:text-white"
                >
                  Refresh list
                </button>
                <button
                  type="button"
                  onClick={onDismissDeleteError}
                  className="rounded-lg px-3 py-1.5 text-xs font-medium text-severity-muted transition-colors hover:text-slate-200"
                >
                  Dismiss
                </button>
                {deleteError.status === 404 && (
                  <span className="text-xs text-severity-muted">
                    That record was already deleted — the list on screen is out
                    of date.
                  </span>
                )}
              </div>
            </div>
          )}

          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="text-xs uppercase tracking-wide text-severity-muted">
                  <th scope="col" className="px-3 py-2 font-medium">
                    Investigation ID
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Application
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Mode
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Provider
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Score
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Created
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <Row
                    key={item.investigation_id}
                    item={item}
                    selected={item.investigation_id === selectedId}
                    onSelect={onSelectRow}
                    confirming={confirmingId === item.investigation_id}
                    onRequestDelete={setConfirmingId}
                    onCancelDelete={() => setConfirmingId(null)}
                    onConfirmDelete={(id) => {
                      setConfirmingId(null)
                      onDelete(id)
                    }}
                    deleting={deletingId === item.investigation_id}
                    // Single-flight: while one delete is in the air every other
                    // row's trash button is inert, so a reader cannot queue a
                    // second destructive call against a list that is about to
                    // be refetched underneath them.
                    deleteDisabled={
                      deletingId !== null &&
                      deletingId !== item.investigation_id
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>

          {items.length === 0 && (
            <p className="border-t border-obsidian-800 px-5 py-8 text-center text-sm text-severity-muted">
              {loading
                ? 'Loading investigations…'
                : page > 1
                  ? 'This page is past the end of the list.'
                  : 'No investigations stored yet.'}
            </p>
          )}

          <div className="flex flex-wrap items-center gap-3 border-t border-obsidian-800 px-5 py-3">
            <button
              type="button"
              onClick={() => onPageChange(page - 1)}
              disabled={page <= 1 || loading}
              className="rounded-lg border border-obsidian-800 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors hover:border-brand-purple/50 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
            >
              Previous
            </button>
            <span className="text-xs text-severity-muted">
              Page {page} of {Math.max(totalPages, 1)}
            </span>
            <button
              type="button"
              onClick={() => onPageChange(page + 1)}
              disabled={page >= totalPages || loading}
              className="rounded-lg border border-obsidian-800 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors hover:border-brand-purple/50 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </>
      )}
    </section>
  )
}

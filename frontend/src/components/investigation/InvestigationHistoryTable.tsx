/**
 * The stored-investigation list: search, then page, then render.
 *
 * The caller owns the fetch and hands over every loaded row; this component
 * owns the filter and the page cursor, because both are views onto that one
 * array and neither means anything outside it. The order is the important part
 * — **filter first, paginate second**. Paginating first and filtering the
 * resulting page would make "no results" mean "not on the page you are looking
 * at", which is a claim about absence the filter has no standing to make.
 *
 * That is also why `useInvestigations` loads the whole table rather than one
 * page: a filter can only search what it holds. Where the load was cut short
 * by its own ceiling, the footer says so, since a search over a silently
 * clipped set is the same problem moved somewhere less visible.
 *
 * Two details of the payload are load-bearing here. `confidence_score` is
 * nullable and `null` means *not measured*, which is a different fact from
 * `0` — the badge says so rather than rendering a zero, and the filter matches
 * it as `n/a` so what is searchable is what is legible. Every other column is
 * optional too, because a run that degraded before recording a provider still
 * has a row worth listing.
 *
 * Deletion is confirmed inline, in the row, rather than in a modal. The row is
 * where the record's identity already is — the id, the application, the score
 * and the date are all on screen and stay on screen — whereas a dialog has to
 * re-state which record it means and can only quote a fragment of it. It also
 * keeps the destructive click and its confirmation in one place instead of
 * moving the pointer to a floating box, and needs no focus trap or escape
 * handling to be accessible.
 */

import { useMemo, useState } from 'react'

import { ErrorEnvelope } from '../common/ErrorEnvelope'
import { Spinner } from '../common/Spinner'
import type { ApiError } from '../../services/api'
import type { InvestigationsSnapshot } from '../../hooks/useInvestigations'
import type { InvestigationItem } from '../../types/api'

/** Rows shown per page of the filtered list. */
const PAGE_SIZE = 10

/**
 * Stands in for `data.items` before the first load settles.
 *
 * A module constant rather than a `?? []` literal at the use site: the literal
 * would be a fresh array on every render, so the memoized filter below would
 * see a changed dependency and re-run each time — re-scanning every loaded row
 * on renders where nothing about the data changed at all.
 */
const NO_ITEMS: InvestigationItem[] = []

/**
 * The searchable text of one row.
 *
 * The rule is that the filter matches **what the row displays**, so a reader
 * can always predict what a query will hit. Hence `confidence_score` is
 * searchable both as its number and as `n/a`, which is what the badge shows
 * when the score is `null` — searching `n/a` finds the unmeasured runs, and
 * that is a genuinely useful query rather than a curiosity.
 *
 * `created_at` is deliberately excluded. It renders through
 * `toLocaleString`, so what a row displays depends on the reader's locale and
 * time zone; a filter over it would match different rows on different
 * machines, and matching the raw ISO string instead would mean matching text
 * that appears nowhere on screen.
 */
function searchableText(item: InvestigationItem): string {
  const score =
    item.confidence_score === null || item.confidence_score === undefined
      ? 'n/a'
      : String(item.confidence_score)

  return [
    item.investigation_id,
    item.application_name,
    item.analysis_mode,
    item.llm_provider,
    score,
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
}

/**
 * Match a row against a query.
 *
 * Split on whitespace and every term must match somewhere in the row, so
 * `openai fast` narrows rather than widens. Each term is a plain substring
 * test: users type fragments of ids (`abfb`) far more often than whole ones,
 * and a prefix-only or word-boundary rule would miss exactly that case.
 */
function matchesQuery(item: InvestigationItem, terms: string[]): boolean {
  if (terms.length === 0) return true
  const haystack = searchableText(item)
  return terms.every((term) => haystack.includes(term))
}

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
  onRefresh,
  selectedId,
  onSelectRow,
  onDelete,
  deletingId = null,
  deleteError = null,
  onDismissDeleteError,
}: {
  data: InvestigationsSnapshot | null
  loading: boolean
  error: ApiError | null
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
  const allItems = data?.items ?? NO_ITEMS
  const total = data?.total ?? 0
  const truncated = data?.truncated ?? false

  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)

  // Which row is asking "are you sure?". One at a time by construction, since
  // it is a single id rather than a set: opening a second confirmation closes
  // the first, so there is never more than one armed destructive button.
  const [confirmingId, setConfirmingId] = useState<string | null>(null)

  const terms = useMemo(
    () => query.trim().toLowerCase().split(/\s+/).filter(Boolean),
    [query],
  )

  const filtered = useMemo(
    () => allItems.filter((item) => matchesQuery(item, terms)),
    [allItems, terms],
  )

  const filtering = terms.length > 0
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE)

  // Clamped rather than reset. Narrowing the query while on page 4 should land
  // on the last page of what is left, not silently on page 1 — and a page that
  // has gone out of range must not render as empty when rows still match.
  // Derived on every render instead of corrected in an effect, so there is no
  // frame in which the pager and the rows disagree.
  const safePage = totalPages === 0 ? 1 : Math.min(page, totalPages)
  const start = (safePage - 1) * PAGE_SIZE
  const items = filtered.slice(start, start + PAGE_SIZE)

  const changePage = (next: number) => {
    setPage(Math.max(1, Math.min(next, Math.max(totalPages, 1))))
  }

  const changeQuery = (next: string) => {
    setQuery(next)
    // The old cursor means nothing against a different result set.
    setPage(1)
    // An armed confirmation belongs to a row that may be filtered out by the
    // next keystroke; disarming it prevents a Delete button surviving into a
    // list where its row is no longer visible.
    setConfirmingId(null)
  }

  return (
    <section className="rounded-xl border border-obsidian-800 bg-obsidian-900 shadow-lg shadow-black/20">
      <div className="border-b border-obsidian-800 px-5 py-4">
        <div className="flex flex-wrap items-center gap-3">
          <div>
            <h2 className="text-base font-semibold text-slate-100">History</h2>
            <p className="text-xs text-severity-muted">
              {/* Two counts while filtering, because "3 investigations" over a
                  filtered list would misreport how much is stored. */}
              {filtering
                ? `${filtered.length} of ${allItems.length} shown`
                : `${total} stored ${
                    total === 1 ? 'investigation' : 'investigations'
                  }`}
              , newest first
            </p>
          </div>
          {loading && <Spinner className="h-4 w-4 text-severity-info" />}
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            className="ml-auto rounded-lg border border-obsidian-800 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors hover:border-brand-purple/50 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            Refresh
          </button>
        </div>

        {/* Filtering is local to rows already in memory, so it runs on every
            keystroke with no debounce: there is no request to spare and a
            delay would only make the table feel slower than it is. */}
        <div className="relative mt-3">
          <span
            className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-severity-muted"
            aria-hidden="true"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              strokeLinecap="round"
              className="h-3.5 w-3.5"
            >
              <circle cx="11" cy="11" r="7" />
              <path d="M20 20l-4.5-4.5" />
            </svg>
          </span>
          <input
            type="search"
            value={query}
            onChange={(event) => changeQuery(event.target.value)}
            // Escape clears, which is what the native `type="search"` clear
            // affordance does in the browsers that draw one — handled here so
            // the behaviour is the same in the ones that do not.
            onKeyDown={(event) => {
              if (event.key === 'Escape') changeQuery('')
            }}
            placeholder="Search id, application, mode, provider or score…"
            aria-label="Search stored investigations"
            className="w-full rounded-lg border border-obsidian-800 bg-obsidian-950 py-2 pl-9 pr-20 text-sm text-slate-200 placeholder:text-severity-muted focus:border-brand-purple focus:outline-none focus:ring-1 focus:ring-brand-purple [&::-webkit-search-cancel-button]:hidden"
          />
          {query && (
            <button
              type="button"
              onClick={() => changeQuery('')}
              className="absolute inset-y-0 right-2 my-1 rounded px-2 text-xs font-medium text-severity-muted transition-colors hover:text-slate-200"
            >
              Clear
            </button>
          )}
        </div>

        {/* Stated whenever the filter is active, not only when it matches
            nothing: a query that returns 12 rows out of a clipped set is just
            as incomplete as one that returns none, and only this line says so. */}
        {truncated && filtering && (
          <p className="mt-2 text-xs text-severity-warn">
            Searching the {allItems.length.toLocaleString()} most recent of{' '}
            {total.toLocaleString()} records. Older ones are not loaded and
            cannot match.
          </p>
        )}
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
            <div className="border-t border-obsidian-800 px-5 py-8 text-center">
              {loading ? (
                <p className="text-sm text-severity-muted">
                  Loading investigations…
                </p>
              ) : filtering ? (
                // "No matches" and "nothing stored" are different facts, and
                // conflating them would tell someone their table was empty
                // when it was only their query that was.
                <>
                  <p className="text-sm text-slate-200">
                    No investigation matches “{query.trim()}”.
                  </p>
                  <button
                    type="button"
                    onClick={() => changeQuery('')}
                    className="mt-2 text-xs font-medium text-brand-purple transition-colors hover:text-brand-violet"
                  >
                    Clear search
                  </button>
                </>
              ) : (
                <p className="text-sm text-severity-muted">
                  No investigations stored yet.
                </p>
              )}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3 border-t border-obsidian-800 px-5 py-3">
            <button
              type="button"
              onClick={() => changePage(safePage - 1)}
              disabled={safePage <= 1 || loading}
              className="rounded-lg border border-obsidian-800 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors hover:border-brand-purple/50 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
            >
              Previous
            </button>
            <span className="text-xs text-severity-muted">
              Page {safePage} of {Math.max(totalPages, 1)}
            </span>
            <button
              type="button"
              onClick={() => changePage(safePage + 1)}
              disabled={safePage >= totalPages || loading}
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

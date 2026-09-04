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
 */

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

function Row({
  item,
  selected,
  onSelect,
}: {
  item: InvestigationItem
  selected: boolean
  onSelect: (id: string) => void
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
      className={`cursor-pointer border-t border-obsidian-800 transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-brand-purple ${
        selected
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
}: {
  data: PaginatedInvestigationsResponse | null
  loading: boolean
  error: ApiError | null
  page: number
  onPageChange: (page: number) => void
  onRefresh: () => void
  selectedId: string | null
  onSelectRow: (id: string) => void
}) {
  const items = data?.items ?? []
  const totalPages = data?.total_pages ?? 0
  const total = data?.total ?? 0

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
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <Row
                    key={item.investigation_id}
                    item={item}
                    selected={item.investigation_id === selectedId}
                    onSelect={onSelectRow}
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

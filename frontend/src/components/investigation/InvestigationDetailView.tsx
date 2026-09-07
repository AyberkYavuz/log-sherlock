/**
 * One selected investigation, inspected.
 *
 * The container that owns `useInvestigationDetail` and everything that can go
 * wrong around it. Two tabs over one payload: the stored report read section by
 * section, and the same report read as the pipeline that produced it.
 *
 * Four states, not two, and the distinction between the first two is the point.
 * A `null` id is *idle* — nothing is selected, so nothing is fetched and
 * nothing is claimed. That is not the same as loading, and it is not the same
 * as an empty report. The hook already draws that line by issuing no request
 * for a `null` id; this component renders nothing at all in that case rather
 * than an empty panel, so the history table below it is undisturbed until a row
 * is actually clicked.
 *
 * The tab state deliberately survives a change of `selectedId`: someone
 * comparing the pipeline shape of three runs in a row should not have to
 * re-select the tab for each one.
 */

import { useState } from 'react'

import { ErrorEnvelope } from '../common/ErrorEnvelope'
import { Spinner } from '../common/Spinner'
import { PipelineGraph } from './PipelineGraph'
import { StructuredReportView } from './StructuredReportView'
import { useInvestigationDetail } from '../../hooks/useInvestigationDetail'

type Tab = 'report' | 'pipeline'

const TABS: { id: Tab; label: string; hint: string }[] = [
  {
    id: 'report',
    label: 'Structured Report',
    hint: 'The stored document, by provenance',
  },
  {
    id: 'pipeline',
    label: 'Pipeline Execution Graph',
    hint: 'Per-stage outcome, inferred from the report',
  },
]

/**
 * Whether the fetched report has any content at all.
 *
 * A row can exist with a `NULL` report, which the API returns as `{}` rather
 * than a 404 — so "the record is there but empty" is a real state and a
 * different one from "no such record". Checking the four sections rather than
 * `Object.keys` length means a document carrying only empty sections still
 * reads as empty.
 */
function isEmptyReport(report: unknown): boolean {
  if (!report || typeof report !== 'object') return true
  const sections = report as Record<string, unknown>
  return !(
    sections.metadata ||
    sections.synthesis ||
    sections.deterministic_outputs ||
    sections.ai_insights
  )
}

export function InvestigationDetailView({
  investigationId,
  onClose,
}: {
  /** The selected row, or `null` when nothing is selected. */
  investigationId: string | null
  /** Collapse the panel — resets the caller's `selectedId`. */
  onClose: () => void
}) {
  const { detail, loading, error, refetch } =
    useInvestigationDetail(investigationId)
  const [tab, setTab] = useState<Tab>('report')

  // Idle. Nothing selected, so nothing is rendered — see the module docstring.
  if (investigationId === null) return null

  const report = detail?.structured_report
  const empty = !!detail && isEmptyReport(report)

  return (
    <section className="rounded-xl border border-obsidian-800 bg-obsidian-900 shadow-lg shadow-black/20">
      <header className="flex flex-wrap items-center gap-3 border-b border-obsidian-800 px-5 py-4">
        <div className="min-w-0">
          <h2 className="flex flex-wrap items-center gap-2 text-base font-semibold text-slate-100">
            Inspection
            <code className="rounded bg-obsidian-950/60 px-2 py-0.5 font-mono text-xs font-normal text-slate-300">
              {investigationId}
            </code>
          </h2>
          <p className="mt-0.5 text-xs text-severity-muted">
            {report?.metadata?.application_name
              ? report.metadata.application_name
              : 'The full stored report, exactly as the graph wrote it.'}
          </p>
        </div>

        <div className="ml-auto flex items-center gap-2">
          {loading && <Spinner className="h-4 w-4 text-severity-info" />}
          <button
            type="button"
            onClick={refetch}
            disabled={loading}
            className="rounded-lg border border-obsidian-800 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors hover:border-brand-purple/50 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            Refresh
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-obsidian-800 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors hover:border-severity-error/50 hover:text-severity-error"
          >
            Close Inspection
          </button>
        </div>
      </header>

      {/* The tab strip is rendered even while loading, so the chrome does not
          reflow when the payload lands and the reader's tab choice stays
          visible across a selection change. */}
      <div
        role="tablist"
        aria-label="Investigation views"
        className="flex flex-wrap gap-1 border-b border-obsidian-800 px-5 py-2"
      >
        {TABS.map((candidate) => {
          const active = tab === candidate.id
          return (
            <button
              key={candidate.id}
              type="button"
              role="tab"
              aria-selected={active}
              title={candidate.hint}
              onClick={() => setTab(candidate.id)}
              className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                active
                  ? 'bg-brand-purple text-white'
                  : 'text-slate-300 hover:bg-obsidian-800/60 hover:text-white'
              }`}
            >
              {candidate.label}
            </button>
          )
        })}
      </div>

      <div className="p-5">
        {error ? (
          <ErrorEnvelope error={error} onRetry={refetch} />
        ) : !detail ? (
          // No payload yet. `loading` is true on a first selection; if it is
          // false here the request settled without data, which the hook only
          // produces for a cancelled request that has since been superseded.
          <div className="flex items-center justify-center gap-3 py-16 text-sm text-severity-muted">
            {loading ? (
              <>
                <Spinner className="h-4 w-4 text-severity-info" />
                Loading the stored report…
              </>
            ) : (
              'Nothing to show for this selection.'
            )}
          </div>
        ) : empty || !report ? (
          <p className="rounded-lg border border-dashed border-obsidian-800 px-4 py-10 text-center text-sm text-severity-muted">
            This record exists but carries no report. That is distinct from a
            missing record, which would have been a 404.
          </p>
        ) : tab === 'report' ? (
          // Keyed on the id so selecting a different investigation remounts
          // the view. Without it React reconciles by position, the panels keep
          // their open/closed flags, and the next report opens with whichever
          // sections the *previous* one had been expanded to — which is the one
          // state a fresh inspection should never start in.
          <StructuredReportView
            key={detail.investigation_id}
            report={report}
            investigationId={detail.investigation_id}
          />
        ) : (
          <PipelineGraph report={report} />
        )}
      </div>
    </section>
  )
}

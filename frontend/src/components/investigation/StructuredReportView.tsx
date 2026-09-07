/**
 * The stored `structured_report`, rendered in five panels.
 *
 * The report is partitioned by *provenance* rather than by topic — measurements
 * in `deterministic_outputs` and `metadata.parser_metrics`, inferences in
 * `ai_insights` and `synthesis` — and that split is the whole design of the
 * document. It survives into this view rather than being flattened into one
 * long page: each panel says whose conclusion it is showing, because a reader
 * of a stored investigation has to be able to tell an arithmetic fact from a
 * model's opinion without knowing which node produced what.
 *
 * Every binding below was verified against the live `investigations` table
 * (`inv-graph-001`, `inv-graph-002`, `inv-graph-003`, `inv-graph-abfb`), which
 * is what settles the awkward cases. Four of them shape this file:
 *
 *   * **`logger_distribution` carries a `null` value** for records that had no
 *     logger — 1,514 of them in `inv-graph-001`. It renders as an explicit
 *     "(no logger)" chip, because dropping the row would silently lose the
 *     majority of that dataset and printing `null` would read as a logger
 *     named "null".
 *   * **`metadata_distributions` values are not all strings.** `statusCode`,
 *     `nights` and `durationMs` are integers. Rendering goes through one
 *     formatter that handles both rather than assuming either.
 *   * **`primary_error_signature_id` can be `null`** — it is on
 *     `inv-graph-003` — and that is a real answer, not a missing one. It is
 *     stated as "no primary cause named" rather than left blank.
 *   * **`investigation_timestamp` can be `""`**, on any record created by a
 *     direct graph run rather than through the API. It shows as "not recorded",
 *     because no node in the graph invents a clock reading.
 *
 * Nothing here re-validates the document. It is served back from JSONB exactly
 * as stored and a report written by an older release must still render, so
 * every section is read defensively and an absent one produces a stated
 * absence rather than a crash.
 */

import { useState } from 'react'

import type {
  AnomalyItem,
  CategoryCount,
  ErrorSignature,
  MilestoneKind,
  ParserMetrics,
  StructuredInvestigationReport,
  TimelineEvent,
} from '../../types/api'

// ---------------------------------------------------------------------------
// Shared formatting
// ---------------------------------------------------------------------------

/**
 * Render a distribution value, whatever type it arrived as.
 *
 * `null` is the meaningful case: in `logger_distribution` it is the bucket for
 * records the parser found no logger on, and it has to read as that rather than
 * as the string "null" or as a dropped row.
 */
function formatCategoryValue(value: unknown): {
  text: string
  absent: boolean
} {
  if (value === null || value === undefined) {
    return { text: '(no logger)', absent: true }
  }
  if (typeof value === 'string') {
    return value.trim()
      ? { text: value, absent: false }
      : { text: '(empty)', absent: true }
  }
  if (typeof value === 'boolean' || typeof value === 'number') {
    return { text: String(value), absent: false }
  }
  return { text: JSON.stringify(value), absent: false }
}

/** The same, for a metadata distribution where "(no logger)" would be wrong. */
function formatMetadataValue(value: unknown): { text: string; absent: boolean } {
  if (value === null || value === undefined) {
    return { text: '(none)', absent: true }
  }
  return formatCategoryValue(value)
}

/** An ISO-8601 instant in the reader's locale, with the original on hover. */
function formatInstant(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'medium',
  })
}

/** Just the clock part, for a dense timeline where the date is in the header. */
function formatClock(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleTimeString(undefined, {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function formatRatio(ratio: number | undefined): string {
  if (ratio === undefined || ratio === null || Number.isNaN(ratio)) return '—'
  return `${(ratio * 100).toFixed(2)}%`
}

// ---------------------------------------------------------------------------
// Shared chrome
// ---------------------------------------------------------------------------

/**
 * One collapsible panel.
 *
 * Collapsible because the five sections differ in size by two orders of
 * magnitude — a parser-metrics grid is eight numbers, a timeline can be
 * hundreds of events — and `provenance` is a required prop rather than an
 * optional flourish: it is the one thing every panel has to state.
 */
function Panel({
  title,
  provenance,
  subtitle,
  children,
  defaultOpen = true,
}: {
  title: string
  provenance: string
  subtitle?: string
  children: React.ReactNode
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <section className="overflow-hidden rounded-xl border border-obsidian-800 bg-obsidian-900">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-5 py-3.5 text-left transition-colors hover:bg-obsidian-800/40"
      >
        <span className="min-w-0">
          <span className="block text-sm font-semibold text-slate-100">
            {title}
          </span>
          <span className="block text-xs text-severity-muted">
            {provenance}
            {subtitle ? ` · ${subtitle}` : ''}
          </span>
        </span>
        <span
          className="ml-auto shrink-0 text-severity-muted transition-transform"
          style={{ transform: open ? 'rotate(90deg)' : undefined }}
          aria-hidden="true"
        >
          ▸
        </span>
      </button>
      {open && (
        <div className="border-t border-obsidian-800 px-5 py-4">{children}</div>
      )}
    </section>
  )
}

/** A stated absence. Never rendered as an empty panel body. */
function Absent({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-severity-muted">{children}</p>
}

/** One labelled figure. */
function Stat({
  label,
  value,
  title,
  tone = 'default',
}: {
  label: string
  value: React.ReactNode
  title?: string
  tone?: 'default' | 'warn' | 'error'
}) {
  const valueTone =
    tone === 'error'
      ? 'text-severity-error'
      : tone === 'warn'
        ? 'text-severity-warn'
        : 'text-slate-100'

  return (
    <div
      className="rounded-lg border border-obsidian-800 bg-obsidian-950/60 px-3 py-2.5"
      title={title}
    >
      <p className="text-[11px] uppercase tracking-wide text-severity-muted">
        {label}
      </p>
      <p className={`mt-0.5 font-mono text-sm font-semibold ${valueTone}`}>
        {value}
      </p>
    </div>
  )
}

/**
 * A horizontal distribution bar list.
 *
 * Shares are computed against the largest row rather than against a total,
 * because a distribution is capped at its top 20 entries and the counts
 * therefore need not sum to the dataset size — normalizing by a total that is
 * not in the payload would draw bars that quietly understate every row.
 */
function DistributionBars({
  rows,
  format = formatCategoryValue,
  emptyLabel,
  limit = 8,
}: {
  rows: CategoryCount[] | undefined
  format?: (value: unknown) => { text: string; absent: boolean }
  emptyLabel: string
  limit?: number
}) {
  if (!rows || rows.length === 0) return <Absent>{emptyLabel}</Absent>

  const shown = rows.slice(0, limit)
  const max = Math.max(...rows.map((row) => row.count), 1)

  return (
    <div className="space-y-1.5">
      {shown.map((row, index) => {
        const { text, absent } = format(row.value)
        return (
          <div key={`${index}-${text}`} className="flex items-center gap-2">
            <span
              className={`w-40 shrink-0 truncate font-mono text-xs ${
                absent ? 'italic text-severity-muted' : 'text-slate-300'
              }`}
              title={text}
            >
              {text}
            </span>
            <span className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-obsidian-950">
              <span
                className="block h-full rounded-full bg-brand-purple/70"
                style={{ width: `${Math.max((row.count / max) * 100, 1.5)}%` }}
              />
            </span>
            <span className="w-16 shrink-0 text-right font-mono text-xs text-severity-muted">
              {row.count.toLocaleString()}
            </span>
          </div>
        )
      })}
      {rows.length > shown.length && (
        <p className="pt-0.5 text-xs text-severity-muted">
          + {rows.length - shown.length} more{' '}
          {rows.length - shown.length === 1 ? 'row' : 'rows'}
        </p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 1. AI Insights
// ---------------------------------------------------------------------------

const ANOMALY_SEVERITY_STYLE: Record<string, string> = {
  critical: 'bg-severity-error/10 text-severity-error ring-severity-error/30',
  warning: 'bg-severity-warn/10 text-severity-warn ring-severity-warn/30',
  info: 'bg-severity-info/10 text-severity-info ring-severity-info/30',
}

/** `category` is a closed vocabulary, so it can be spelled for a reader. */
const ANOMALY_CATEGORY_LABEL: Record<string, string> = {
  volume_spike: 'Volume spike',
  logger_cascade: 'Logger cascade',
  metadata_clustering: 'Metadata clustering',
  baseline_shift: 'Baseline shift',
}

function AnomalyCard({ anomaly }: { anomaly: AnomalyItem }) {
  const severityStyle =
    ANOMALY_SEVERITY_STYLE[anomaly.severity] ??
    'bg-severity-muted/10 text-severity-muted ring-severity-muted/30'

  return (
    <li className="rounded-lg border border-obsidian-800 bg-obsidian-950/60 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-semibold text-slate-200">
          {ANOMALY_CATEGORY_LABEL[anomaly.category] ?? anomaly.category}
        </span>
        <span
          className={`rounded-md px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide ring-1 ring-inset ${severityStyle}`}
        >
          {anomaly.severity}
        </span>
        {/* `null` means the anomaly is a property of the whole dataset rather
            than of a moment in it, which is a different statement from an
            unknown window — so it is spelled out instead of omitted. */}
        <span className="ml-auto font-mono text-[11px] text-severity-muted">
          {anomaly.time_window ?? 'whole dataset'}
        </span>
      </div>

      <p className="mt-2 text-sm leading-relaxed text-slate-200">
        {anomaly.description}
      </p>

      {anomaly.affected_loggers && anomaly.affected_loggers.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] uppercase tracking-wide text-severity-muted">
            Loggers
          </span>
          {anomaly.affected_loggers.map((logger) => (
            <code
              key={logger}
              className="rounded bg-obsidian-800 px-1.5 py-0.5 font-mono text-[11px] text-slate-300"
            >
              {logger}
            </code>
          ))}
        </div>
      )}
    </li>
  )
}

/**
 * One fingerprinted failure.
 *
 * The template is shown in monospace because it is *masked* text — `<IP>`,
 * `<NUM>`, `<UUID>` are placeholders the fingerprinting pass substituted so two
 * occurrences of one failure collapse to identical text. The unmasked
 * `sample_messages` sit underneath, which is the pairing that makes the
 * template legible: the template says what recurred, the samples say what the
 * real values were.
 *
 * An empty `explanation` is left out rather than rendered as a blank line. It
 * is the model's field and keeps its `""` default when the reasoning pass
 * degraded, so absence means "not reasoned about" — the panel header says so
 * once instead of every card repeating it.
 */
function SignatureCard({
  signature,
  isPrimary,
}: {
  signature: ErrorSignature
  isPrimary: boolean
}) {
  const [open, setOpen] = useState(false)
  const isError = /^(error|critical|fatal|severe|emergency|exception)$/i.test(
    signature.severity,
  )

  return (
    <li
      className={`rounded-lg border bg-obsidian-950/60 p-3 ${
        isPrimary
          ? 'border-severity-error/50 ring-1 ring-inset ring-severity-error/20'
          : 'border-obsidian-800'
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <code className="font-mono text-xs font-semibold text-brand-purple">
          {signature.signature_id}
        </code>
        <span
          className={`rounded px-1.5 py-0.5 font-mono text-[11px] font-medium ${
            isError
              ? 'bg-severity-error/10 text-severity-error'
              : 'bg-severity-warn/10 text-severity-warn'
          }`}
        >
          {signature.severity}
        </span>
        {isPrimary && (
          <span className="rounded bg-severity-error/15 px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-severity-error">
            Primary cause
          </span>
        )}
        {/* Only meaningful when the model actually ran: it defaults to `false`,
            so it is shown as a positive flag and never as "ruled out". */}
        {!isPrimary && signature.is_root_cause_candidate && (
          <span className="rounded bg-severity-warn/15 px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide text-severity-warn">
            Candidate
          </span>
        )}
        <span className="ml-auto font-mono text-xs text-severity-muted">
          ×{signature.count.toLocaleString()}
        </span>
      </div>

      <p className="mt-2 break-words font-mono text-xs leading-relaxed text-slate-200">
        {signature.template}
      </p>

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-severity-muted">
        <span title={signature.first_seen ?? undefined}>
          first {formatInstant(signature.first_seen)}
        </span>
        <span title={signature.last_seen ?? undefined}>
          last {formatInstant(signature.last_seen)}
        </span>
        {signature.loggers.length > 0 ? (
          <span className="font-mono">{signature.loggers.join(', ')}</span>
        ) : (
          // Observed empty on the plain-text records: a collated traceback
          // often carries no logger at all.
          <span className="italic">no logger attributed</span>
        )}
      </div>

      {signature.explanation.trim() && (
        <p className="mt-2 border-t border-obsidian-800 pt-2 text-sm leading-relaxed text-slate-300">
          {signature.explanation}
        </p>
      )}

      {signature.sample_messages.length > 0 && (
        <>
          <button
            type="button"
            onClick={() => setOpen((current) => !current)}
            aria-expanded={open}
            className="mt-2 text-[11px] font-medium text-brand-purple transition-colors hover:text-brand-violet"
          >
            {open ? 'Hide' : 'Show'} {signature.sample_messages.length} unmasked
            sample
            {signature.sample_messages.length === 1 ? '' : 's'}
          </button>
          {open && (
            <ul className="mt-1.5 space-y-1">
              {signature.sample_messages.map((message, index) => (
                <li
                  key={`${index}-${message.slice(0, 24)}`}
                  className="overflow-x-auto whitespace-pre rounded bg-obsidian-950 p-2 font-mono text-[11px] leading-relaxed text-slate-400"
                >
                  {message}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </li>
  )
}

function AiInsightsPanel({ report }: { report: StructuredInvestigationReport }) {
  const synthesis = report.synthesis ?? {}
  const errorSummary = report.ai_insights?.error_summary
  const patternSummary = report.ai_insights?.pattern_summary

  const rootCause = synthesis.root_cause?.trim()
  const executiveSummary = synthesis.executive_summary?.trim()

  // The fixed fallback text is worded to be unmistakably an absence rather than
  // a finding, so it is rendered as one: amber and captioned, never as a
  // confident purple headline.
  const isFallbackRootCause = !!rootCause
    ?.toLowerCase()
    .startsWith('root cause undetermined')

  return (
    <Panel
      title="AI Insights"
      provenance="Inference — what three models concluded"
      subtitle="synthesis · ai_insights"
    >
      <div className="space-y-5">
        {/* -- synthesis.root_cause ---------------------------------------- */}
        <div>
          <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-severity-muted">
            Root cause
            <code className="ml-2 font-mono normal-case tracking-normal">
              synthesis.root_cause
            </code>
          </h4>
          {rootCause ? (
            <div
              className={`rounded-lg border-l-4 p-4 ${
                isFallbackRootCause
                  ? 'border-severity-warn bg-severity-warn/10'
                  : 'border-brand-purple bg-brand-purple/10'
              }`}
            >
              <p
                className={`text-base font-medium leading-relaxed ${
                  isFallbackRootCause
                    ? 'text-severity-warn'
                    : 'text-slate-100'
                }`}
              >
                {rootCause}
              </p>
              {isFallbackRootCause && (
                <p className="mt-2 text-xs text-severity-muted">
                  This is the fallback text, not a finding: the synthesis pass
                  could not reach a model. Every deterministic section of this
                  report is unaffected and complete.
                </p>
              )}
            </div>
          ) : (
            <Absent>No root cause was recorded for this investigation.</Absent>
          )}
        </div>

        {/* -- synthesis.executive_summary --------------------------------- */}
        <div>
          <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-severity-muted">
            Executive summary
            <code className="ml-2 font-mono normal-case tracking-normal">
              synthesis.executive_summary
            </code>
          </h4>
          {executiveSummary ? (
            <div className="space-y-3 rounded-lg border border-obsidian-800 bg-obsidian-950/60 p-4">
              {executiveSummary.split(/\n\s*\n/).map((paragraph, index) => (
                <p
                  key={index}
                  className="whitespace-pre-line text-sm leading-relaxed text-slate-300"
                >
                  {paragraph}
                </p>
              ))}
            </div>
          ) : (
            <Absent>No executive summary was recorded.</Absent>
          )}
        </div>

        {/* -- ai_insights.error_summary ----------------------------------- */}
        <div className="border-t border-obsidian-800 pt-4">
          <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-severity-muted">
            Error analysis
            <code className="ml-2 font-mono normal-case tracking-normal">
              ai_insights.error_summary
            </code>
          </h4>

          {errorSummary ? (
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                <Stat
                  label="Entries analyzed"
                  value={errorSummary.total_errors_analyzed.toLocaleString()}
                />
                <Stat
                  label="Unique signatures"
                  value={errorSummary.unique_signatures_found.toLocaleString()}
                />
                <Stat
                  label="Primary signature"
                  value={
                    errorSummary.primary_error_signature_id ?? 'none named'
                  }
                  tone={
                    errorSummary.primary_error_signature_id ? 'default' : 'warn'
                  }
                  title={
                    errorSummary.primary_error_signature_id
                      ? undefined
                      : 'The model named no root cause. This is a real answer, ' +
                        'and it is the largest single penalty against the ' +
                        'confidence score.'
                  }
                />
              </div>

              {errorSummary.cascading_impact_summary?.trim() && (
                <div className="rounded-lg border border-obsidian-800 bg-obsidian-950/60 p-3">
                  <p className="text-[11px] uppercase tracking-wide text-severity-muted">
                    Cascading impact
                  </p>
                  <p className="mt-1 text-sm leading-relaxed text-slate-300">
                    {errorSummary.cascading_impact_summary}
                  </p>
                </div>
              )}

              {errorSummary.signatures?.length ? (
                <ul className="space-y-2">
                  {errorSummary.signatures.map((signature) => (
                    <SignatureCard
                      key={signature.signature_id}
                      signature={signature}
                      isPrimary={
                        !!errorSummary.primary_error_signature_id &&
                        signature.signature_id ===
                          errorSummary.primary_error_signature_id
                      }
                    />
                  ))}
                </ul>
              ) : (
                <Absent>
                  No error signatures were fingerprinted — the payload carried
                  no error- or warning-level entries.
                </Absent>
              )}
            </div>
          ) : (
            <Absent>This report carries no error analysis.</Absent>
          )}
        </div>

        {/* -- ai_insights.pattern_summary --------------------------------- */}
        <div className="border-t border-obsidian-800 pt-4">
          <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-severity-muted">
            Pattern analysis
            <code className="ml-2 font-mono normal-case tracking-normal">
              ai_insights.pattern_summary
            </code>
          </h4>

          {patternSummary ? (
            <div className="space-y-3">
              {patternSummary.behavioral_synthesis?.trim() && (
                <div className="rounded-lg border border-obsidian-800 bg-obsidian-950/60 p-3">
                  <p className="text-[11px] uppercase tracking-wide text-severity-muted">
                    Behavioral synthesis
                  </p>
                  <p className="mt-1 text-sm leading-relaxed text-slate-300">
                    {patternSummary.behavioral_synthesis}
                  </p>
                </div>
              )}

              <div>
                <p className="mb-1.5 text-[11px] uppercase tracking-wide text-severity-muted">
                  Anomalies ({patternSummary.anomalies?.length ?? 0})
                </p>
                {patternSummary.anomalies?.length ? (
                  <ul className="space-y-2">
                    {patternSummary.anomalies.map((anomaly, index) => (
                      <AnomalyCard
                        key={`${anomaly.category}-${index}`}
                        anomaly={anomaly}
                      />
                    ))}
                  </ul>
                ) : (
                  // An empty list is the expected answer for a payload that
                  // behaved normally, so it is stated as a conclusion.
                  <Absent>
                    No anomalies were reported. For a payload that behaved
                    normally this is the expected answer, not a gap.
                  </Absent>
                )}
              </div>

              {patternSummary.cross_logger_correlations?.length ? (
                <div>
                  <p className="mb-1.5 text-[11px] uppercase tracking-wide text-severity-muted">
                    Cross-logger correlations
                  </p>
                  <ul className="space-y-1.5">
                    {patternSummary.cross_logger_correlations.map(
                      (line, index) => (
                        <li
                          key={index}
                          className="border-l-2 border-obsidian-800 pl-3 text-sm leading-relaxed text-slate-300"
                        >
                          {line}
                        </li>
                      ),
                    )}
                  </ul>
                </div>
              ) : null}

              {patternSummary.metadata_insights?.length ? (
                <div>
                  <p className="mb-1.5 text-[11px] uppercase tracking-wide text-severity-muted">
                    Metadata insights
                  </p>
                  <ul className="space-y-1.5">
                    {patternSummary.metadata_insights.map((line, index) => (
                      <li
                        key={index}
                        className="border-l-2 border-obsidian-800 pl-3 text-sm leading-relaxed text-slate-300"
                      >
                        {line}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          ) : (
            <Absent>This report carries no pattern analysis.</Absent>
          )}
        </div>
      </div>
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// 2. Investigation Notes
// ---------------------------------------------------------------------------

/**
 * A note is a degradation when a node is reporting what it could *not* do.
 *
 * Matched on the wording the nodes actually emit — "LLM reasoning unavailable",
 * "Data Quality Warning", "skipped", "omitted" — and the same pattern the run
 * form uses, so one run reads identically live and when read back later.
 */
const DEGRADATION_PATTERN =
  /unavailable|could not|failed|warning|skipped|omitted|missing/i

function InvestigationNotesPanel({
  notes,
}: {
  notes: string[] | undefined
}) {
  const list = notes ?? []
  const degraded = list.filter((note) => DEGRADATION_PATTERN.test(note)).length

  return (
    <Panel
      title="Investigation Notes"
      provenance="Runtime record — what each node said about its own limits"
      subtitle={
        list.length
          ? `${list.length} notes, ${degraded} flagged`
          : 'no notes recorded'
      }
    >
      {list.length === 0 ? (
        <Absent>
          No notes were recorded. Nodes write a note only when there is
          something to report, so this is ordinary rather than suspicious.
        </Absent>
      ) : (
        <>
          <p className="mb-3 text-xs leading-relaxed text-severity-muted">
            A snapshot of the upstream notes as they stood when the report was
            assembled. Every LLM node in this pipeline degrades rather than
            fails, so a run can come back complete-looking with its
            interpretation silently missing — these lines are the only place
            that shows.
          </p>
          <ul className="space-y-1.5">
            {list.map((note, index) => {
              const flagged = DEGRADATION_PATTERN.test(note)
              return (
                <li
                  // Notes are free text from up to eight nodes and can
                  // legitimately repeat, so the index is the only stable
                  // identity available.
                  key={`${index}-${note.slice(0, 24)}`}
                  className={`flex gap-2 rounded-lg px-3 py-2 text-xs leading-relaxed ${
                    flagged
                      ? 'bg-severity-warn/10 text-severity-warn'
                      : 'bg-obsidian-950/60 text-slate-300'
                  }`}
                >
                  <span className="shrink-0 font-mono text-severity-muted">
                    {String(index + 1).padStart(2, '0')}
                  </span>
                  <span className="min-w-0">{note}</span>
                </li>
              )
            })}
          </ul>
        </>
      )}
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// 3. Parser Metrics
// ---------------------------------------------------------------------------

function ParserMetricsPanel({ metrics }: { metrics: ParserMetrics | undefined }) {
  if (!metrics) {
    return (
      <Panel
        title="Parser Metrics"
        provenance="Measurement — ingestion health"
        subtitle="metadata.parser_metrics"
      >
        <Absent>This report carries no parser metrics.</Absent>
      </Panel>
    )
  }

  // The two counts that cost the confidence score a ratio penalty. Non-zero is
  // worth colouring; zero is the clean case and stays neutral.
  const malformedTone = metrics.malformed_lines > 0 ? 'error' : 'default'
  const missingTone = metrics.missing_timestamp_lines > 0 ? 'warn' : 'default'
  // Below 0.80 the scoring engine deducts 10 points, so that is the line here
  // too. Exactly 0.80 is good enough — the penalty applies strictly below it.
  const confidenceTone = metrics.parser_confidence < 0.8 ? 'warn' : 'default'

  return (
    <Panel
      title="Parser Metrics"
      provenance="Measurement — ingestion health"
      subtitle="metadata.parser_metrics"
    >
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Parser" value={metrics.parser_name} />
        <Stat
          label="Detected format"
          value={metrics.detected_format}
        />
        <Stat
          label="Confidence"
          value={metrics.parser_confidence.toFixed(2)}
          tone={confidenceTone}
          title={
            confidenceTone === 'warn'
              ? 'Below 0.80, which costs the confidence score 10 points — the ' +
                'format was largely a guess.'
              : undefined
          }
        />
        <Stat label="Total lines" value={metrics.total_lines.toLocaleString()} />
        <Stat
          label="Parsed lines"
          value={metrics.parsed_lines.toLocaleString()}
        />
        <Stat label="Blank lines" value={metrics.blank_lines.toLocaleString()} />
        <Stat
          label="Malformed lines"
          value={metrics.malformed_lines.toLocaleString()}
          tone={malformedTone}
          title="Non-blank lines that could not be parsed. These contributed nothing to any downstream analysis."
        />
        <Stat
          label="Missing timestamps"
          value={metrics.missing_timestamp_lines.toLocaleString()}
          tone={missingTone}
          title="Parsed entries with no timestamp. They still reached the statistics and the error fingerprinting; only the timeline could not place them."
        />
      </div>

      <p className="mt-3 text-xs leading-relaxed text-severity-muted">
        The invariant{' '}
        <code className="font-mono">
          total = blank + parsed + malformed
        </code>{' '}
        holds: {metrics.total_lines.toLocaleString()} ={' '}
        {metrics.blank_lines.toLocaleString()} +{' '}
        {metrics.parsed_lines.toLocaleString()} +{' '}
        {metrics.malformed_lines.toLocaleString()}.
      </p>
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// 4. Metadata
// ---------------------------------------------------------------------------

/**
 * The deterministic confidence score, as a ring.
 *
 * `null` is rendered as "n/a" rather than as an empty ring, because `null`
 * means *not measured* and `0` means *measured as zero* — a distinction the
 * database, the API and the record list all preserve, and one this indicator
 * must not be the place that collapses.
 */
function ConfidenceRing({ score }: { score: number | null | undefined }) {
  const measured = score !== null && score !== undefined
  const value = measured ? Math.max(0, Math.min(100, score)) : 0

  const color = !measured
    ? '#6B7280'
    : value >= 80
      ? '#34D399'
      : value >= 50
        ? '#FBBF24'
        : '#F87171'

  // A conic gradient rather than an SVG arc: one element, no path arithmetic,
  // and the mask punches the middle out so the ring sits on any background.
  return (
    <div className="flex items-center gap-4">
      <div
        className="relative grid h-24 w-24 shrink-0 place-items-center rounded-full"
        style={{
          background: `conic-gradient(${color} ${value * 3.6}deg, #1F2937 0deg)`,
        }}
        role="img"
        aria-label={
          measured
            ? `Confidence score ${value} of 100`
            : 'Confidence score not measured'
        }
      >
        <div className="grid h-[76px] w-[76px] place-items-center rounded-full bg-obsidian-900">
          <span
            className="font-mono text-xl font-bold"
            style={{ color }}
          >
            {measured ? value : 'n/a'}
          </span>
        </div>
      </div>

      <div className="min-w-0">
        <p className="text-sm font-semibold text-slate-100">
          Confidence score
        </p>
        <p className="mt-1 text-xs leading-relaxed text-severity-muted">
          {measured
            ? 'Computed by arithmetic over parser health and the error ' +
              'analysis, before anything was asked of a model — so it does ' +
              'not depend on whether the synthesis call succeeded.'
            : 'Not measured for this run. This is a different fact from a ' +
              'score of zero, and the two are never collapsed.'}
        </p>
      </div>
    </div>
  )
}

function MetadataPanel({
  metadata,
  investigationId,
}: {
  metadata: StructuredInvestigationReport['metadata'] | undefined
  investigationId: string
}) {
  const meta = metadata ?? {}
  // Observed as `""` on records created by a direct graph run rather than
  // through the API, because no node in the graph invents a clock reading.
  const timestamp = meta.investigation_timestamp?.trim()

  return (
    <Panel
      title="Metadata"
      provenance="Run identity — the reproducibility record"
      subtitle="metadata"
    >
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat
            label="Application"
            value={meta.application_name || '—'}
          />
          <Stat
            label="Investigation ID"
            value={investigationId}
            title="The primary key of the stored row. It lives on the row and in the API response, not inside the report document."
          />
          <Stat
            label="Analysis mode"
            value={meta.analysis_mode || '—'}
            title="The normalized reasoning tier this run used."
          />
          <Stat
            label="LLM provider"
            value={meta.llm_provider || '—'}
            title="The normalized vendor — 'anthropic' where the caller typed 'Claude'."
          />
        </div>

        <div>
          <p className="text-[11px] uppercase tracking-wide text-severity-muted">
            Investigation timestamp
          </p>
          {timestamp ? (
            <p className="mt-0.5 font-mono text-sm text-slate-200" title={timestamp}>
              {formatInstant(timestamp)}
            </p>
          ) : (
            <p className="mt-0.5 text-sm text-severity-muted">
              Not recorded — this investigation was created by a direct graph
              run, which supplies no clock reading. Records created through the
              API carry a real one.
            </p>
          )}
        </div>

        <div className="border-t border-obsidian-800 pt-4">
          <ConfidenceRing score={meta.confidence_score} />
        </div>
      </div>
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// 5. Deterministic Outputs
// ---------------------------------------------------------------------------

/** How each milestone reads to someone who has not read the graph docs. */
const MILESTONE_LABEL: Record<MilestoneKind, string> = {
  logs_start: 'Logs start',
  first_error: 'First error',
  error_onset: 'Error onset',
  peak_error_volume: 'Peak error volume',
  recovery_onset: 'Recovery onset',
  last_error: 'Last error',
  logs_end: 'Logs end',
}

/**
 * Milestones that mark a failure are red; the two coverage bounds are neutral;
 * recovery is the one piece of good news in the vocabulary and is emerald.
 */
const MILESTONE_STYLE: Record<MilestoneKind, string> = {
  logs_start: 'bg-severity-info/10 text-severity-info',
  logs_end: 'bg-severity-info/10 text-severity-info',
  first_error: 'bg-severity-error/10 text-severity-error',
  last_error: 'bg-severity-error/10 text-severity-error',
  error_onset: 'bg-severity-error/10 text-severity-error',
  peak_error_volume: 'bg-severity-error/15 text-severity-error',
  recovery_onset: 'bg-severity-success/10 text-severity-success',
}

function TimelineEventRow({ event }: { event: TimelineEvent }) {
  const isMilestone = event.event_type === 'milestone'
  const kind = event.milestone_kind as MilestoneKind | null | undefined
  const badgeStyle =
    (kind && MILESTONE_STYLE[kind]) ?? 'bg-severity-muted/10 text-severity-muted'

  return (
    <li className="relative pl-6">
      {/* The rail dot. Milestones sit on the line as filled markers, buckets as
          hollow ones, so the narrative moments are scannable in a long series. */}
      <span
        className={`absolute left-0 top-1.5 h-2.5 w-2.5 rounded-full border-2 ${
          isMilestone
            ? 'border-brand-purple bg-brand-purple'
            : 'border-obsidian-800 bg-obsidian-950'
        }`}
      />

      <div className="rounded-lg border border-obsidian-800 bg-obsidian-950/60 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <code
            className="font-mono text-xs text-slate-300"
            title={event.timestamp}
          >
            {formatClock(event.timestamp)}
            {/* A bucket's window is half-open, and showing both ends is what
                makes the series readable as contiguous windows rather than as
                instants. */}
            {event.end_timestamp && (
              <span className="text-severity-muted">
                {' → '}
                {formatClock(event.end_timestamp)}
              </span>
            )}
          </code>

          {isMilestone && kind ? (
            <span
              className={`rounded px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${badgeStyle}`}
            >
              {MILESTONE_LABEL[kind] ?? kind}
            </span>
          ) : (
            <span className="rounded bg-obsidian-800 px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide text-severity-muted">
              bucket
            </span>
          )}

          <span className="ml-auto flex items-center gap-2 font-mono text-[11px]">
            <span className="text-severity-muted">
              {(event.total_logs ?? 0).toLocaleString()} logs
            </span>
            {!!event.error_count && (
              <span className="text-severity-error">
                {event.error_count.toLocaleString()} err
              </span>
            )}
            {!!event.warning_count && (
              <span className="text-severity-warn">
                {event.warning_count.toLocaleString()} warn
              </span>
            )}
          </span>
        </div>

        {event.summary && (
          <p className="mt-1.5 text-xs leading-relaxed text-slate-300">
            {event.summary}
          </p>
        )}

        {event.top_loggers && event.top_loggers.length > 0 && (
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] text-severity-muted">
              Top loggers
            </span>
            {event.top_loggers.map((logger) => (
              <code
                key={logger}
                className="rounded bg-obsidian-800 px-1.5 py-0.5 font-mono text-[11px] text-slate-300"
              >
                {logger}
              </code>
            ))}
          </div>
        )}

        {event.sample_messages && event.sample_messages.length > 0 && (
          <ul className="mt-1.5 space-y-1">
            {event.sample_messages.map((message, index) => (
              <li
                key={`${index}-${message.slice(0, 24)}`}
                className="truncate font-mono text-[11px] text-severity-muted"
                title={message}
              >
                {message}
              </li>
            ))}
          </ul>
        )}
      </div>
    </li>
  )
}

/** How many timeline events render before the rest are behind a button. */
const TIMELINE_PAGE = 20

function DeterministicOutputsPanel({
  report,
}: {
  report: StructuredInvestigationReport
}) {
  const statistics = report.deterministic_outputs?.statistics
  const timeline = report.deterministic_outputs?.timeline
  const [showAll, setShowAll] = useState(false)

  const events = timeline ?? []
  const shown = showAll ? events : events.slice(0, TIMELINE_PAGE)
  const metadataKeys = Object.keys(statistics?.metadata_distributions ?? {})

  return (
    <Panel
      title="Deterministic Outputs"
      provenance="Measurement — arithmetic, reproducible from the same logs"
      subtitle="deterministic_outputs"
    >
      <div className="space-y-5">
        {/* -- statistics --------------------------------------------------- */}
        <div>
          <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-severity-muted">
            Statistics
            <code className="ml-2 font-mono normal-case tracking-normal">
              deterministic_outputs.statistics
            </code>
          </h4>

          {statistics ? (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Stat
                  label="Errors"
                  value={(
                    statistics.severity?.error_count ?? 0
                  ).toLocaleString()}
                  tone={statistics.severity?.error_count ? 'error' : 'default'}
                />
                <Stat
                  label="Error ratio"
                  value={formatRatio(statistics.severity?.error_ratio)}
                  title="A share of the whole dataset — records with no level are in the denominator."
                />
                <Stat
                  label="Warnings"
                  value={(
                    statistics.severity?.warning_count ?? 0
                  ).toLocaleString()}
                  tone={statistics.severity?.warning_count ? 'warn' : 'default'}
                />
                <Stat
                  label="Warning ratio"
                  value={formatRatio(statistics.severity?.warning_ratio)}
                />
              </div>

              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Stat
                  label="With timestamp"
                  value={(
                    statistics.timestamp_coverage?.with_timestamp ?? 0
                  ).toLocaleString()}
                />
                <Stat
                  label="Without timestamp"
                  value={(
                    statistics.timestamp_coverage?.without_timestamp ?? 0
                  ).toLocaleString()}
                  tone={
                    statistics.timestamp_coverage?.without_timestamp
                      ? 'warn'
                      : 'default'
                  }
                />
                <Stat
                  label="Earliest"
                  value={formatClock(statistics.timestamp_coverage?.earliest)}
                  title={statistics.timestamp_coverage?.earliest ?? undefined}
                />
                <Stat
                  label="Latest"
                  value={formatClock(statistics.timestamp_coverage?.latest)}
                  title={statistics.timestamp_coverage?.latest ?? undefined}
                />
              </div>

              <div className="grid gap-4 lg:grid-cols-2">
                <div>
                  <p className="mb-1.5 text-[11px] uppercase tracking-wide text-severity-muted">
                    Level distribution
                  </p>
                  <DistributionBars
                    rows={statistics.level_distribution}
                    emptyLabel="No levels were recorded."
                  />
                </div>
                <div>
                  <p className="mb-1.5 text-[11px] uppercase tracking-wide text-severity-muted">
                    Logger distribution
                  </p>
                  <DistributionBars
                    rows={statistics.logger_distribution}
                    emptyLabel="No loggers were recorded."
                  />
                </div>
              </div>

              {metadataKeys.length > 0 && (
                <div>
                  <p className="mb-1.5 text-[11px] uppercase tracking-wide text-severity-muted">
                    Metadata distributions ({metadataKeys.length} keys)
                  </p>
                  <p className="mb-2 text-xs leading-relaxed text-severity-muted">
                    Discovered from the records themselves — no field name is
                    hard-coded, so these differ per log ecosystem.
                  </p>
                  <div className="grid gap-3 lg:grid-cols-2">
                    {metadataKeys.map((key) => (
                      <div
                        key={key}
                        className="rounded-lg border border-obsidian-800 bg-obsidian-950/40 p-3"
                      >
                        <p className="mb-1.5 font-mono text-xs font-semibold text-slate-200">
                          {key}
                        </p>
                        <DistributionBars
                          rows={statistics.metadata_distributions[key]}
                          format={formatMetadataValue}
                          emptyLabel="No values."
                          limit={5}
                        />
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <Absent>This report carries no statistics.</Absent>
          )}
        </div>

        {/* -- timeline ----------------------------------------------------- */}
        <div className="border-t border-obsidian-800 pt-4">
          <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-severity-muted">
            Timeline
            <code className="ml-2 font-mono normal-case tracking-normal">
              deterministic_outputs.timeline
            </code>
          </h4>

          {events.length === 0 ? (
            <Absent>
              The timeline is empty — nothing in the payload could be placed on
              a time axis. Entries the parser could not stamp are excluded and
              never guessed into place.
            </Absent>
          ) : (
            <>
              <p className="mb-3 text-xs leading-relaxed text-severity-muted">
                {events.length} events, strictly ordered by time. Empty windows
                are dropped from the series, so it is ordered but not
                contiguous.
              </p>
              <ol className="space-y-2 border-l border-obsidian-800 pl-2">
                {shown.map((event, index) => (
                  <TimelineEventRow
                    key={`${event.timestamp}-${event.event_type}-${event.milestone_kind ?? index}`}
                    event={event}
                  />
                ))}
              </ol>
              {events.length > TIMELINE_PAGE && (
                <button
                  type="button"
                  onClick={() => setShowAll((current) => !current)}
                  className="mt-3 rounded-lg border border-obsidian-800 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors hover:border-brand-purple/50 hover:text-white"
                >
                  {showAll
                    ? `Show first ${TIMELINE_PAGE}`
                    : `Show all ${events.length} events`}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// The view
// ---------------------------------------------------------------------------

/**
 * Ordered inference first, measurement second — the reverse of how the graph
 * builds them, and deliberately so. A reader opening a stored investigation
 * wants the conclusion and the caveats on it; the arithmetic it rests on is
 * what they scroll to when they want to check that conclusion.
 */
export function StructuredReportView({
  report,
  investigationId,
}: {
  report: StructuredInvestigationReport
  investigationId: string
}) {
  return (
    <div className="space-y-3">
      <AiInsightsPanel report={report} />
      <InvestigationNotesPanel notes={report.synthesis?.investigation_notes} />
      <ParserMetricsPanel metrics={report.metadata?.parser_metrics} />
      <MetadataPanel
        metadata={report.metadata}
        investigationId={investigationId}
      />
      <DeterministicOutputsPanel report={report} />
    </div>
  )
}

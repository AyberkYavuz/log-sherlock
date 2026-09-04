/**
 * The submission form for one investigation.
 *
 * It owns `useRunInvestigation` rather than receiving the state, because the
 * run and the feedback about the run belong together: the button's disabled
 * state, the notes and the failure envelope are all the same event seen from
 * three angles. The parent hears about the outcome through `onCompleted`, which
 * is all it needs to refresh the history.
 *
 * The result panel is the part worth reading twice. `db_persisted: false` is a
 * *success* — the analysis ran and only its storage failed — so it is amber
 * rather than red, and the notes underneath are the only place the reason
 * appears. Every LLM node degrades rather than raises, which means a run can
 * come back complete-looking with its interpretation silently missing; the
 * notes are the sole evidence of that, so they are shown rather than hidden
 * behind a toggle.
 */

import { useState } from 'react'

import { useRunInvestigation } from '../../hooks/useRunInvestigation'
import { ErrorEnvelope } from '../common/ErrorEnvelope'
import { Spinner } from '../common/Spinner'
import type {
  AnalysisMode,
  InvestigateRequest,
  InvestigateResponse,
  LLMProvider,
} from '../../types/api'

const ANALYSIS_MODES: AnalysisMode[] = ['fast', 'standard', 'deep']

const PROVIDERS: { value: LLMProvider; label: string }[] = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'gemini', label: 'Gemini' },
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'local', label: 'Local (OpenAI-compatible)' },
]

/** The column width of the `application_name` column, enforced server-side. */
const MAX_APPLICATION_NAME = 255

/**
 * Four Pino JSON lines carrying one ERROR, so a first run produces a real
 * signature instead of an empty report. Handy for checking the wiring without
 * hunting for a file; `sample_logs/` holds the full incident fixtures.
 */
const SAMPLE_LOGS = [
  '{"level":30,"time":"2026-07-29T10:59:25.610Z","name":"api","msg":"Incoming request POST /bookings"}',
  '{"level":40,"time":"2026-07-29T10:59:25.808Z","name":"payment","msg":"Payment provider slow: 1840ms"}',
  '{"level":50,"time":"2026-07-29T10:59:26.144Z","name":"payment","msg":"Payment provider unavailable: connection refused to 10.0.4.12:8443"}',
  '{"level":30,"time":"2026-07-29T10:59:26.150Z","name":"api","msg":"Request completed with status 503"}',
].join('\n')

/**
 * A note is a degradation when a node is reporting what it could *not* do.
 * Matched on the wording the nodes actually emit — "LLM reasoning unavailable",
 * "Data Quality Warning", "could not persist" — so those lines are legible at a
 * glance instead of being buried in a list of successes.
 */
const DEGRADATION_PATTERN = /unavailable|could not|failed|warning|skipped|omitted/i

function FieldLabel({
  htmlFor,
  children,
  hint,
}: {
  htmlFor: string
  children: React.ReactNode
  hint?: React.ReactNode
}) {
  return (
    <div className="mb-1.5 flex items-baseline justify-between gap-3">
      <label
        htmlFor={htmlFor}
        className="text-xs font-medium uppercase tracking-wide text-slate-300"
      >
        {children}
      </label>
      {hint && <span className="text-xs text-severity-muted">{hint}</span>}
    </div>
  )
}

function RunResult({ result }: { result: InvestigateResponse }) {
  const persisted = result.db_persisted

  return (
    <div
      className={`rounded-lg border p-4 ${
        persisted
          ? 'border-severity-success/40 bg-severity-success/10'
          : 'border-severity-warn/40 bg-severity-warn/10'
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`h-2 w-2 shrink-0 rounded-full ${
            persisted ? 'bg-severity-success' : 'bg-severity-warn'
          }`}
        />
        <span
          className={`text-sm font-semibold ${
            persisted ? 'text-severity-success' : 'text-severity-warn'
          }`}
        >
          {persisted ? 'Investigation stored' : 'Analysis ran, not stored'}
        </span>
        <code className="ml-auto rounded bg-obsidian-950/60 px-2 py-0.5 font-mono text-xs text-slate-300">
          {result.investigation_id}
        </code>
      </div>

      {!persisted && (
        <p className="mt-2 text-sm text-slate-200">
          The pipeline completed and every finding is intact; only the write to
          PostgreSQL failed. The notes below say why.
        </p>
      )}

      {result.investigation_notes.length > 0 && (
        <ul className="mt-3 max-h-56 space-y-1.5 overflow-auto pr-1">
          {result.investigation_notes.map((note, index) => (
            <li
              // Notes are free text from eight nodes and can legitimately
              // repeat, so the index is the only stable identity available.
              key={`${index}-${note.slice(0, 24)}`}
              className={`text-xs leading-relaxed ${
                DEGRADATION_PATTERN.test(note)
                  ? 'text-severity-warn'
                  : 'text-severity-muted'
              }`}
            >
              {note}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function InvestigationForm({
  onCompleted,
}: {
  /** Called after a run returns, so the caller can refresh what it shows. */
  onCompleted?: (result: InvestigateResponse) => void
}) {
  const [applicationName, setApplicationName] = useState('')
  const [rawLogs, setRawLogs] = useState('')
  const [analysisMode, setAnalysisMode] = useState<AnalysisMode>('standard')
  const [provider, setProvider] = useState<LLMProvider>('local')
  const [enableWebSearch, setEnableWebSearch] = useState(false)

  const run = useRunInvestigation()

  // The backend rejects a blank value on either field with a 422; checking here
  // turns that into a disabled button rather than a round trip that fails.
  const trimmedName = applicationName.trim()
  const trimmedLogs = rawLogs.trim()
  const canSubmit = !!trimmedName && !!trimmedLogs && !run.loading

  const lineCount = rawLogs ? rawLogs.split('\n').length : 0

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return

    const payload: InvestigateRequest = {
      application_name: trimmedName,
      raw_logs: rawLogs,
      analysis_mode: analysisMode,
      llm_provider: provider,
      enable_web_search: enableWebSearch,
    }

    const result = await run.execute(payload)
    // `null` means the request failed or was superseded; `run.error` already
    // carries the reason and the envelope below renders it.
    if (result) onCompleted?.(result)
  }

  return (
    <section className="rounded-xl border border-obsidian-800 bg-obsidian-900 p-5 shadow-lg shadow-black/20 sm:p-6">
      <h2 className="text-base font-semibold text-slate-100">
        New investigation
      </h2>
      <p className="mt-1 text-xs text-severity-muted">
        Paste raw log output. The pipeline parses it, analyses it and stores the
        report.
      </p>

      <form onSubmit={handleSubmit} className="mt-5 space-y-5">
        <div>
          <FieldLabel htmlFor="application_name" hint="required">
            Application name
          </FieldLabel>
          <input
            id="application_name"
            type="text"
            value={applicationName}
            onChange={(event) => setApplicationName(event.target.value)}
            maxLength={MAX_APPLICATION_NAME}
            placeholder="payment-service"
            autoComplete="off"
            className="w-full rounded-lg border border-obsidian-800 bg-obsidian-950 px-3 py-2 text-sm text-slate-200 placeholder:text-severity-muted focus:border-brand-purple focus:outline-none focus:ring-1 focus:ring-brand-purple"
          />
        </div>

        <div>
          <FieldLabel
            htmlFor="raw_logs"
            hint={
              <>
                {lineCount} {lineCount === 1 ? 'line' : 'lines'} ·{' '}
                {rawLogs.length.toLocaleString()} chars
              </>
            }
          >
            Raw logs
          </FieldLabel>
          <textarea
            id="raw_logs"
            value={rawLogs}
            onChange={(event) => setRawLogs(event.target.value)}
            rows={12}
            spellCheck={false}
            // Log lines are long and are meant to be read one per row, so they
            // scroll sideways rather than wrapping — a wrapped stack trace is
            // unreadable and makes the line count disagree with what is on
            // screen.
            wrap="off"
            placeholder='{"level":50,"time":"2026-07-29T10:59:26.144Z","name":"payment","msg":"..."}'
            className="w-full resize-y overflow-auto whitespace-pre rounded-lg border border-obsidian-800 bg-obsidian-950 p-3 font-mono text-xs leading-relaxed text-slate-200 placeholder:text-severity-muted focus:border-brand-purple focus:outline-none focus:ring-1 focus:ring-brand-purple"
          />
          <button
            type="button"
            onClick={() => setRawLogs(SAMPLE_LOGS)}
            className="mt-1.5 text-xs text-brand-purple transition-colors hover:text-brand-violet"
          >
            Insert sample logs
          </button>
        </div>

        <div>
          <FieldLabel htmlFor="analysis_mode">Analysis mode</FieldLabel>
          <div
            id="analysis_mode"
            role="group"
            aria-label="Analysis mode"
            className="inline-flex w-full rounded-lg border border-obsidian-800 bg-obsidian-950 p-1"
          >
            {ANALYSIS_MODES.map((mode) => (
              <button
                key={mode}
                type="button"
                onClick={() => setAnalysisMode(mode)}
                aria-pressed={analysisMode === mode}
                className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium capitalize transition-colors ${
                  analysisMode === mode
                    ? 'bg-brand-purple text-white'
                    : 'text-slate-300 hover:text-white'
                }`}
              >
                {mode}
              </button>
            ))}
          </div>
        </div>

        <div>
          <FieldLabel htmlFor="llm_provider">LLM provider</FieldLabel>
          <select
            id="llm_provider"
            value={provider}
            onChange={(event) =>
              setProvider(event.target.value as LLMProvider)
            }
            className="w-full rounded-lg border border-obsidian-800 bg-obsidian-950 px-3 py-2 text-sm text-slate-200 focus:border-brand-purple focus:outline-none focus:ring-1 focus:ring-brand-purple"
          >
            {PROVIDERS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <label className="flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            checked={enableWebSearch}
            onChange={(event) => setEnableWebSearch(event.target.checked)}
            // `accent-color` rather than Tailwind's colour utilities: those
            // style a *replaced* checkbox and need the forms plugin, so without
            // it a native checkbox stays stock white against a dark panel.
            className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer accent-brand-purple focus:outline-none focus-visible:ring-1 focus-visible:ring-brand-purple"
          />
          <span>
            <span className="text-sm text-slate-200">Enable web search</span>
            <span className="block text-xs text-severity-muted">
              Looks up documentation for unfamiliar error signatures. Off by
              default — it trades latency and cost for coverage.
            </span>
          </span>
        </label>

        <button
          type="submit"
          disabled={!canSubmit}
          className="flex w-full items-center justify-center gap-2 rounded-lg bg-brand-purple px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-brand-violet focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-purple focus-visible:ring-offset-2 focus-visible:ring-offset-obsidian-900 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {run.loading && <Spinner />}
          {run.loading ? 'Investigating…' : 'Start Investigation'}
        </button>

        {run.loading && (
          <p className="text-center text-xs text-severity-muted">
            Running all eight nodes. A large payload can take minutes.
          </p>
        )}
      </form>

      {(run.error || run.data) && (
        <div className="mt-5">
          {run.error ? (
            <ErrorEnvelope error={run.error} />
          ) : (
            run.data && <RunResult result={run.data} />
          )}
        </div>
      )}
    </section>
  )
}

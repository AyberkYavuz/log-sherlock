/**
 * Temporary API test bench.
 *
 * Exercises all four hooks against a live backend so the service layer can be
 * verified before any real view is built: press a button, see either the parsed
 * payload or the error envelope the backend actually returned. Replaced by the
 * real shell in the next step.
 */

import { useState } from 'react'

import { useHealthCheck } from './hooks/useHealthCheck'
import { useInvestigationDetail } from './hooks/useInvestigationDetail'
import { useInvestigations } from './hooks/useInvestigations'
import { useRunInvestigation } from './hooks/useRunInvestigation'
import type { ApiError } from './services/api'
import type { InvestigateRequest } from './types/api'

/** Which result the viewer is currently showing. */
type Panel = 'health' | 'history' | 'run' | 'detail'

/**
 * Four Pino JSON lines carrying one ERROR, so the run produces a real error
 * signature rather than an empty report. Small on purpose — this is a wiring
 * check, not a benchmark; `sample_logs/` holds the full incident fixtures.
 */
const SAMPLE_LOGS = [
  '{"level":30,"time":"2026-07-29T10:59:25.610Z","name":"api","msg":"Incoming request POST /bookings"}',
  '{"level":40,"time":"2026-07-29T10:59:25.808Z","name":"payment","msg":"Payment provider slow: 1840ms"}',
  '{"level":50,"time":"2026-07-29T10:59:26.144Z","name":"payment","msg":"Payment provider unavailable: connection refused to 10.0.4.12:8443"}',
  '{"level":30,"time":"2026-07-29T10:59:26.150Z","name":"api","msg":"Request completed with status 503"}',
].join('\n')

/**
 * `local` targets the project's mock LLM (`python3 -m uvicorn
 * tests.mock_local_llm:app --port 8080`) so the bench needs no provider key. It
 * works without the mock too: every LLM node degrades rather than fails, and
 * the reason lands in `investigation_notes` — which is itself worth seeing.
 */
const SAMPLE_REQUEST: InvestigateRequest = {
  application_name: 'frontend-test-bench',
  raw_logs: SAMPLE_LOGS,
  analysis_mode: 'fast',
  llm_provider: 'local',
  enable_web_search: false,
}

function ActionButton({
  onClick,
  active,
  disabled,
  children,
}: {
  onClick: () => void
  active: boolean
  disabled?: boolean
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={[
        'rounded-lg px-4 py-2 text-sm font-medium transition-colors',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-purple',
        'focus-visible:ring-offset-2 focus-visible:ring-offset-obsidian-950',
        'disabled:cursor-not-allowed disabled:opacity-40',
        active
          ? 'bg-brand-purple text-white hover:bg-brand-violet'
          : 'border border-obsidian-800 bg-obsidian-900 text-slate-300 hover:border-brand-purple/50 hover:text-white',
      ].join(' ')}
    >
      {children}
    </button>
  )
}

/** A red envelope: the status, the unwrapped message, and the raw `detail`. */
function ErrorEnvelope({ error }: { error: ApiError }) {
  return (
    <div className="rounded-lg border border-severity-error/40 bg-severity-error/10 p-4">
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 shrink-0 rounded-full bg-severity-error" />
        <span className="font-mono text-xs font-semibold uppercase tracking-wide text-severity-error">
          {error.status ? `HTTP ${error.status}` : 'Network error'}
        </span>
        {error.isRetryable && (
          <span className="rounded bg-severity-warn/10 px-2 py-0.5 text-[11px] font-medium text-severity-warn">
            retryable
          </span>
        )}
      </div>
      <p className="mt-2 text-sm text-slate-200">{error.message}</p>
      {error.detail != null && (
        <pre className="mt-3 max-h-64 overflow-auto rounded bg-obsidian-950/70 p-3 text-xs leading-relaxed text-severity-error/90">
          {JSON.stringify(error.detail, null, 2)}
        </pre>
      )}
    </div>
  )
}

function JsonViewer({ value }: { value: unknown }) {
  return (
    <pre className="max-h-[28rem] overflow-auto rounded-lg bg-obsidian-950/70 p-4 text-xs leading-relaxed text-slate-300">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

/** One shape for every panel: pending, failed, empty, or a payload. */
function ResultBody({
  loading,
  error,
  data,
  pendingNote,
  emptyNote,
}: {
  loading: boolean
  error: ApiError | null
  data: unknown
  pendingNote: string
  emptyNote: string
}) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-obsidian-800 bg-obsidian-950/50 p-4 text-sm text-severity-info">
        <span className="h-2 w-2 animate-pulse rounded-full bg-severity-info" />
        {pendingNote}
      </div>
    )
  }
  if (error) return <ErrorEnvelope error={error} />
  if (data == null) {
    return <p className="text-sm text-severity-muted">{emptyNote}</p>
  }
  return <JsonViewer value={data} />
}

function App() {
  const [panel, setPanel] = useState<Panel>('health')
  const [detailId, setDetailId] = useState<string | null>(null)

  const health = useHealthCheck()
  const history = useInvestigations({ initialPage: 1, limit: 10 })
  const detail = useInvestigationDetail(detailId)
  const run = useRunInvestigation()

  // The most recently created record, falling back to the newest row the list
  // knows about — whichever exists is a real id the detail endpoint will accept.
  const candidateId =
    run.data?.investigation_id ??
    history.data?.items[0]?.investigation_id ??
    null

  const handleHealth = () => {
    setPanel('health')
    health.refetch()
  }

  const handleHistory = () => {
    setPanel('history')
    // Changing the page is itself a refetch, so asking for both would issue two
    // requests for the same rows.
    if (history.page === 1) history.refetch()
    else history.setPage(1)
  }

  const handleRun = () => {
    setPanel('run')
    void run.execute(SAMPLE_REQUEST)
  }

  const handleDetail = () => {
    if (!candidateId) return
    setPanel('detail')
    if (detailId === candidateId) detail.refetch()
    else setDetailId(candidateId)
  }

  const backendUp = !!health.health && !health.error

  return (
    <div className="min-h-full bg-obsidian-950">
      <header className="border-b border-obsidian-800">
        <div className="mx-auto flex max-w-5xl items-center gap-3 px-6 py-4">
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-purple font-mono text-sm font-bold text-white">
            LS
          </span>
          <div className="leading-tight">
            <h1 className="text-lg font-semibold tracking-tight text-slate-100">
              log-sherlock
            </h1>
            <p className="text-xs text-severity-muted">API test bench</p>
          </div>

          <span className="ml-auto flex items-center gap-2 rounded-full border border-obsidian-800 px-3 py-1 text-xs">
            <span
              className={`h-2 w-2 rounded-full ${
                health.loading
                  ? 'animate-pulse bg-severity-info'
                  : backendUp
                    ? 'bg-severity-success'
                    : 'bg-severity-error'
              }`}
            />
            <span
              className={
                health.loading
                  ? 'text-severity-info'
                  : backendUp
                    ? 'text-severity-success'
                    : 'text-severity-error'
              }
            >
              {health.loading
                ? 'checking'
                : backendUp
                  ? 'backend up'
                  : 'backend unreachable'}
            </span>
          </span>
        </div>
      </header>

      <main className="mx-auto max-w-5xl space-y-6 px-6 py-10">
        <div className="flex flex-wrap gap-3">
          <ActionButton onClick={handleHealth} active={panel === 'health'}>
            Check Health
          </ActionButton>
          <ActionButton onClick={handleHistory} active={panel === 'history'}>
            Fetch History (Page 1)
          </ActionButton>
          <ActionButton
            onClick={handleRun}
            active={panel === 'run'}
            disabled={run.loading}
          >
            {run.loading ? 'Running…' : 'Run Mock Investigation'}
          </ActionButton>
          <ActionButton
            onClick={handleDetail}
            active={panel === 'detail'}
            disabled={!candidateId}
          >
            Fetch Detail
            {candidateId ? ` (${candidateId})` : ' (no id yet)'}
          </ActionButton>
        </div>

        <section className="rounded-xl border border-obsidian-800 bg-obsidian-900 p-6 shadow-lg shadow-black/20">
          {panel === 'health' && (
            <>
              <PanelHeading
                title="GET /api/health"
                note="Touches nothing — no database, no graph."
              />
              <ResultBody
                loading={health.loading}
                error={health.error}
                data={health.health}
                pendingNote="Checking the backend…"
                emptyNote="No result yet."
              />
            </>
          )}

          {panel === 'history' && (
            <>
              <PanelHeading
                title="POST /api/investigations"
                note={`Page ${history.page}, limit ${history.limit}. Metadata only — the report is excluded at the SQL projection.`}
              />
              <ResultBody
                loading={history.loading}
                error={history.error}
                data={history.data}
                pendingNote="Reading the record list…"
                emptyNote="No result yet."
              />
            </>
          )}

          {panel === 'run' && (
            <>
              <PanelHeading
                title="POST /api/investigate"
                note="Runs all eight nodes. db_persisted: false is still a 200 — read investigation_notes for why."
              />
              <ResultBody
                loading={run.loading}
                error={run.error}
                data={run.data}
                pendingNote="Running the pipeline — this can take minutes."
                emptyNote="Not run yet."
              />
            </>
          )}

          {panel === 'detail' && (
            <>
              <PanelHeading
                title="POST /api/investigations/{id}"
                note="The whole structured_report, verbatim from JSONB."
              />
              <ResultBody
                loading={detail.loading}
                error={detail.error}
                data={detail.detail}
                pendingNote="Reading the stored report…"
                emptyNote="Nothing selected."
              />
            </>
          )}
        </section>

        <p className="text-xs text-severity-muted">
          Requests go to <code className="text-slate-400">/api</code>, proxied to{' '}
          <code className="text-slate-400">127.0.0.1:8010</code>. The mock run
          uses <code className="text-slate-400">llm_provider: "local"</code>, so
          no provider key is needed.
        </p>
      </main>
    </div>
  )
}

function PanelHeading({ title, note }: { title: string; note: string }) {
  return (
    <div className="mb-4">
      <h2 className="font-mono text-sm font-semibold text-slate-100">{title}</h2>
      <p className="mt-1 text-xs text-severity-muted">{note}</p>
    </div>
  )
}

export default App

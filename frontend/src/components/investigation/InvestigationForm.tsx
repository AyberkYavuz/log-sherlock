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
 *
 * Logs reach `raw_logs` three ways — a dropped file, a picked file, or typing —
 * and all three write the same state, because the API takes log *text* and has
 * no notion of a file. A file is a convenience for getting text into the box,
 * not a second kind of input, so nothing downstream of this component can tell
 * which route was used.
 */

import { useMemo, useRef, useState } from 'react'

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

/** `investigation_id` shares that column width and the same server-side check. */
const MAX_INVESTIGATION_ID = 255

/**
 * Extensions the picker offers and a drop is checked against.
 *
 * Matched on the extension rather than on `File.type`, because the browser
 * reports `""` for `.log` on every platform — a MIME check would reject the
 * project's own `sample_logs/*.log` fixtures, which are the files most likely
 * to be dropped here.
 *
 * `.json` is deliberately **not** accepted, and dropping it costs nothing: the
 * API takes log *text*, and a JSON-lines corpus is still a log file. Every
 * JSON-shaped fixture in `sample_logs/` is named for what it is — `json.log`,
 * `java_spring_boot_large.json.log` — so all of them still pass this check. A
 * bare `.json` file, by contrast, is almost always a single pretty-printed
 * document rather than one object per line, which the parser would read as one
 * unparseable entry. Refusing it at the picker is a better answer than a
 * completed investigation over a single malformed line.
 */
const ACCEPTED_EXTENSIONS = ['.txt', '.log'] as const

/** The `accept` attribute, and the hint shown under the drop zone. */
const ACCEPT_ATTRIBUTE = ACCEPTED_EXTENSIONS.join(',')

/**
 * Refused above this size, in bytes.
 *
 * Raised from 8 MB to 16 MB, and the number is set by the *browser* rather than
 * by anything on the wire. Four candidate limits were measured; three of them
 * turned out not to bind:
 *
 *   * **The backend.** FastAPI, uvicorn and h11 impose no body-size ceiling and
 *     none is configured in `backend/`, so there is no server-side cap to
 *     respect at all.
 *   * **The Vite dev proxy.** It streams the body rather than buffering it, and
 *     its only relevant bound is the 15-minute timeout in `vite.config.ts`.
 *   * **JSON expansion.** `raw_logs` crosses the wire inside a JSON body, and
 *     escaping measured at 1.003x for plain-text corpora and 1.142x for
 *     JSON-lines ones, so 16 MB of logs is at most ~18.3 MB on the wire.
 *   * **JS-side work.** Measured on the 3.5 MB Spring Boot benchmark scaled up:
 *     `split('\n')` costs ~4 ms and `JSON.stringify` ~37 ms at 21 MB. Neither
 *     is a wall.
 *
 * What does bind is DOM text layout. The value lives in a controlled
 * `<textarea>`, so the browser holds its own copy and re-lays the text out when
 * React reassigns `value`. That cost is not measurable in Node and it grows
 * faster than the heap does. 16 MB keeps the peak transient footprint bounded —
 * ~32 MB for the UTF-16 string, a comparable DOM copy, and a ~37 MB body string
 * that is alive only across the `JSON.stringify` in `handleSubmit` — while
 * clearing the largest corpus in `sample_logs/` (3.48 MB) by 4.6x.
 *
 * Typing into a loaded 16 MB payload *is* sluggish, and that is the accepted
 * trade rather than an oversight: a file that size is loaded to be submitted,
 * not edited, and Clear stays responsive either way. Files above the ceiling are
 * rejected with their real size and pointed at the API, so the message never
 * implies the limit is the pipeline's.
 */
const MAX_FILE_BYTES = 16 * 1024 * 1024

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function hasAcceptedExtension(name: string): boolean {
  const lower = name.toLowerCase()
  return ACCEPTED_EXTENSIONS.some((extension) => lower.endsWith(extension))
}

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

/** What a successfully read file left behind, for the confirmation line. */
interface LoadedFile {
  name: string
  bytes: number
}

/**
 * The drop zone and file picker, which are one control with two triggers.
 *
 * `dragDepth` is a counter rather than a boolean because `dragenter` and
 * `dragleave` both fire when the pointer crosses into a *child* element, so a
 * boolean flickers off as the cursor moves over the label inside the zone. The
 * counter only reaches zero when the pointer has genuinely left.
 *
 * `onDragOver` must call `preventDefault` on every event, not just once: it is
 * what marks the element a drop target, and without it the browser navigates
 * away to the dropped file and discards everything typed into the form.
 */
function LogFileDropZone({
  onFileText,
  onError,
  disabled,
}: {
  onFileText: (text: string, file: LoadedFile) => void
  onError: (message: string) => void
  disabled: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragDepth, setDragDepth] = useState(0)
  const [reading, setReading] = useState(false)

  const active = dragDepth > 0

  const readFile = async (file: File) => {
    if (!hasAcceptedExtension(file.name)) {
      onError(
        `${file.name} is not a supported log file. Accepted extensions are ` +
          `${ACCEPTED_EXTENSIONS.join(', ')}.`,
      )
      return
    }
    if (file.size > MAX_FILE_BYTES) {
      onError(
        `${file.name} is ${formatBytes(file.size)}, which is over the ` +
          `${formatBytes(MAX_FILE_BYTES)} limit of this editor by ` +
          `${formatBytes(file.size - MAX_FILE_BYTES)}. The limit is the ` +
          'browser’s, not the pipeline’s — the analysis itself has no ' +
          'size ceiling. Either split the file, or post it straight to ' +
          'POST /api/investigate for a full-corpus run.',
      )
      return
    }
    if (file.size === 0) {
      onError(`${file.name} is empty, so there is nothing to analyse.`)
      return
    }

    setReading(true)
    try {
      // `File.text()` decodes as UTF-8, which is what every fixture in
      // `sample_logs/` is. A file in another encoding still loads; its
      // non-ASCII bytes become replacement characters, and the parser treats
      // them as message text like any other character.
      const text = await file.text()
      onFileText(text, { name: file.name, bytes: file.size })
    } catch (cause) {
      onError(
        `${file.name} could not be read ` +
          `(${cause instanceof Error ? cause.message : String(cause)}).`,
      )
    } finally {
      setReading(false)
    }
  }

  const handleDrop = (event: React.DragEvent) => {
    event.preventDefault()
    setDragDepth(0)
    if (disabled) return
    // Only the first file: `raw_logs` is one payload, and silently
    // concatenating several would interleave unrelated ecosystems and make the
    // parser's format detection pick whichever won the sample.
    const file = event.dataTransfer.files?.[0]
    if (file) void readFile(file)
  }

  return (
    <div>
      <div
        onDragEnter={(event) => {
          event.preventDefault()
          setDragDepth((depth) => depth + 1)
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={(event) => {
          event.preventDefault()
          setDragDepth((depth) => Math.max(0, depth - 1))
        }}
        onDrop={handleDrop}
        className={`rounded-lg border border-dashed px-4 py-5 text-center transition-colors ${
          active
            ? 'border-brand-purple bg-brand-purple/10'
            : 'border-obsidian-800 bg-obsidian-950/60'
        }`}
      >
        <input
          ref={inputRef}
          id="log_file"
          type="file"
          accept={ACCEPT_ATTRIBUTE}
          disabled={disabled}
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file) void readFile(file)
            // Cleared so picking the same file twice fires `change` again —
            // the value is unchanged otherwise and the second pick is a no-op.
            event.target.value = ''
          }}
          className="sr-only"
        />
        <p className="text-sm text-slate-200">
          {reading ? (
            <span className="inline-flex items-center gap-2">
              <Spinner className="h-3.5 w-3.5 text-severity-info" />
              Reading file…
            </span>
          ) : (
            <>
              Drop a log file here, or{' '}
              <button
                type="button"
                onClick={() => inputRef.current?.click()}
                disabled={disabled}
                className="font-medium text-brand-purple underline-offset-2 transition-colors hover:text-brand-violet hover:underline disabled:cursor-not-allowed disabled:opacity-40"
              >
                browse
              </button>
            </>
          )}
        </p>
        <p className="mt-1 text-xs text-severity-muted">
          {ACCEPTED_EXTENSIONS.join(', ')} · up to{' '}
          {formatBytes(MAX_FILE_BYTES)}
        </p>
      </div>
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
  const [investigationId, setInvestigationId] = useState('')
  const [rawLogs, setRawLogs] = useState('')
  const [analysisMode, setAnalysisMode] = useState<AnalysisMode>('standard')
  const [provider, setProvider] = useState<LLMProvider>('local')
  const [enableWebSearch, setEnableWebSearch] = useState(false)
  // Which file the current text came from, and why the last one did not load.
  // Both are cleared as soon as the text is edited by any other route, because
  // "loaded incident.log" stops being true the moment the box is retyped.
  const [loadedFile, setLoadedFile] = useState<LoadedFile | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)

  const run = useRunInvestigation()

  // The backend rejects a blank value on either field with a 422; checking here
  // turns that into a disabled button rather than a round trip that fails.
  const trimmedName = applicationName.trim()
  const trimmedLogs = rawLogs.trim()
  const trimmedId = investigationId.trim()
  const canSubmit = !!trimmedName && !!trimmedLogs && !run.loading

  // Memoized because it is O(n) in the payload and the ceiling is now 16 MB:
  // unmemoized this allocated an array of every line on every render, so
  // toggling a checkbox re-split 60,000 lines. Keyed on `rawLogs`, so typing
  // still pays for exactly one split per edit.
  const lineCount = useMemo(
    () => (rawLogs ? rawLogs.split('\n').length : 0),
    [rawLogs],
  )

  /** Adopt file text as the payload, replacing whatever was in the box. */
  const handleFileText = (text: string, file: LoadedFile) => {
    setRawLogs(text)
    setLoadedFile(file)
    setFileError(null)
  }

  /**
   * Typing, pasting or inserting the sample all take this path.
   *
   * Dropping the `loadedFile` attribution here is the point: the line count and
   * character count below are derived from `rawLogs` and stay correct on their
   * own, but a filename is a claim about provenance and goes stale the instant
   * the text is edited.
   */
  const handleRawLogsChange = (text: string) => {
    setRawLogs(text)
    setLoadedFile(null)
    setFileError(null)
  }

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return

    const payload: InvestigateRequest = {
      application_name: trimmedName,
      raw_logs: rawLogs,
      analysis_mode: analysisMode,
      llm_provider: provider,
      enable_web_search: enableWebSearch,
      // Omitted entirely when blank rather than sent as `""`. The backend
      // treats a supplied id as authoritative and never replaces it, so an
      // empty string would be a *supplied* empty id rather than a request to
      // generate one — and the request model would 422 on it.
      ...(trimmedId ? { investigation_id: trimmedId } : {}),
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
          <FieldLabel htmlFor="investigation_id" hint="optional">
            Custom ID
          </FieldLabel>
          <input
            id="investigation_id"
            type="text"
            value={investigationId}
            onChange={(event) => setInvestigationId(event.target.value)}
            maxLength={MAX_INVESTIGATION_ID}
            placeholder="Leave blank to generate one"
            autoComplete="off"
            spellCheck={false}
            aria-describedby="investigation_id_hint"
            className="w-full rounded-lg border border-obsidian-800 bg-obsidian-950 px-3 py-2 font-mono text-sm text-slate-200 placeholder:font-sans placeholder:text-severity-muted focus:border-brand-purple focus:outline-none focus:ring-1 focus:ring-brand-purple"
          />
          {/* The consequence is stated because it is not guessable and it is
              not small: the write is an upsert keyed on this value, so reusing
              an id *overwrites* that investigation rather than being rejected.
              That is the feature — it is what makes a re-run correct a stored
              row — but it is also the way to lose a report by accident. */}
          <p
            id="investigation_id_hint"
            className="mt-1.5 text-xs leading-relaxed text-severity-muted"
          >
            Re-running with an id that already exists replaces that stored
            investigation. Left blank, the backend generates one and reports it
            below — convenient, but it mints a new row on every run.
          </p>
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

          <div className="mb-2 space-y-2">
            <LogFileDropZone
              onFileText={handleFileText}
              onError={(message) => {
                setFileError(message)
                setLoadedFile(null)
              }}
              disabled={run.loading}
            />

            {fileError && (
              <p
                role="alert"
                className="rounded-lg border border-severity-error/40 bg-severity-error/10 px-3 py-2 text-xs text-severity-error"
              >
                {fileError}
              </p>
            )}

            {loadedFile && (
              <p className="flex flex-wrap items-center gap-2 rounded-lg border border-severity-success/40 bg-severity-success/10 px-3 py-2 text-xs text-severity-success">
                <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-severity-success" />
                Loaded <code className="font-mono">{loadedFile.name}</code> ·{' '}
                {formatBytes(loadedFile.bytes)}
                <button
                  type="button"
                  onClick={() => handleRawLogsChange('')}
                  className="ml-auto font-medium text-severity-muted transition-colors hover:text-slate-200"
                >
                  Clear
                </button>
              </p>
            )}
          </div>

          <textarea
            id="raw_logs"
            value={rawLogs}
            onChange={(event) => handleRawLogsChange(event.target.value)}
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
            onClick={() => handleRawLogsChange(SAMPLE_LOGS)}
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

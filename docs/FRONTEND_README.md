# LogSherlock Frontend

The React client for the LogSherlock analysis pipeline. It submits investigations
to the backend, lists and searches what the graph stored, and renders one
investigation's report two ways — as the stored document, and as the pipeline
that produced it.

This document covers the frontend only. The HTTP contract it consumes is
documented in [`BACKEND_README.md`](BACKEND_README.md), the graph behind that
contract in [`GRAPH_README.md`](GRAPH_README.md), and nothing here duplicates
either.

---

## Contents

- [Overview & Architecture](#overview--architecture)
- [Key UI Capabilities](#key-ui-capabilities)
- [Local Setup & Development Commands](#local-setup--development-commands)
- [Frontend Step 6 Test Results Matrix](#frontend-step-6-test-results-matrix)
- [Verification](#verification)

---

## Overview & Architecture

### Technical stack

| Layer | Choice | Notes |
| --- | --- | --- |
| **UI runtime** | React 19 (`react`, `react-dom` ^19.2.8) | Function components and hooks only; no class components, no state library |
| **Language** | TypeScript ~6.0, strict project references | `tsconfig.app.json` for `src/`, `tsconfig.node.json` for the Vite config |
| **Build & dev server** | Vite ^8.2 with `@vitejs/plugin-react` | Dev proxy in front of the FastAPI server — see below |
| **Styling** | Tailwind CSS ^3.4 + PostCSS + Autoprefixer | Custom theme in `tailwind.config.js`; no component library |
| **Graph canvas** | `@xyflow/react` (React Flow) ^12.11 | Only the Pipeline Execution Graph uses it |
| **Linting** | `oxlint` ^1.79 with the `react`, `typescript` and `oxc` plugins | `react/rules-of-hooks` is an error |

> **A correction to the brief.** The specification for this document named
> **Lucide React** as the icon library. It is not a dependency of this project
> and never has been: `frontend/package.json` lists exactly three runtime
> dependencies — `react`, `react-dom` and `@xyflow/react`. Every icon in the UI
> is a hand-authored inline `<svg>` (`TrashIcon` and the search glyph in
> `InvestigationHistoryTable.tsx`, the spinner in `common/Spinner.tsx`), and the
> disclosure chevron on a collapsible panel is the literal character `▸`. That
> is a deliberate trade: the app draws four icons in total, and a dependency
> would cost more than the four paths it replaced. The stack table above
> describes what is actually installed.

### Theme

`tailwind.config.js` extends the default palette with three named scales rather
than reaching for stock Tailwind colours, and the naming is load-bearing:

| Scale | Values | Meaning |
| --- | --- | --- |
| `obsidian` | `950` `#0B0F17`, `900` `#111827`, `800` `#1F2937` | A depth scale, not a colour ramp: canvas, raised surface, divider |
| `brand` | `purple` `#8B5CF6`, `violet` `#7C3AED` | Accent, and the pressed/hover state of the accent |
| `severity` | `error` `#F87171`, `warn` `#FBBF24`, `success` `#34D399`, `info` `#38BDF8`, `muted` `#6B7280` | The vocabulary the backend already speaks |

The severity names match the graph's own — ERROR / WARN / INFO levels, and
info / warning / critical anomaly tiers — so a component maps a payload value
onto a class without inventing a second vocabulary in between. `PipelineGraph`
is the one place these appear as hex literals (`STATUS_COLOR`), because React
Flow renders node borders and edge strokes through inline styles and SVG
attributes, which cannot take a class.

### Where it sits

```
   Browser  →  Vite dev server :5173
                   │
                   │  /api/*  proxied, same-origin, 15-minute timeout
                   ▼
             FastAPI  http://127.0.0.1:8010
                   │
                   ▼
             graph.py  →  PostgreSQL  investigations
```

`vite.config.ts` proxies `/api` to `127.0.0.1:8010`. Port 5173 is already one of
the four origins in the backend's CORS allow-list, so a direct call would work
too — the proxy is still the better default, because it makes every request
same-origin in development and lets the client use relative `/api/...` paths
that stay correct in production behind a single reverse proxy. The proxy's
`timeout` and `proxyTimeout` are both raised to 15 minutes: `POST /api/investigate`
runs the whole pipeline against a payload of the caller's choosing, and the
backend's own deadline (`API_GRAPH_TIMEOUT`) is 900 seconds. A shorter proxy
timeout would fail the request in the browser while an analysis that goes on to
store perfectly well was still running.

`services/api.ts` reads `VITE_API_BASE_URL` and falls back to `/api`, which is
the override for a deployment that serves the two from different origins.

### Source layout

```
frontend/
├── index.html                        # the single mount point
├── vite.config.ts                    # dev server + /api proxy
├── tailwind.config.js                # the Obsidian & Purple theme
├── .oxlintrc.json                    # lint plugins and rules
└── src/
    ├── main.tsx                      # createRoot + StrictMode
    ├── index.css                     # Tailwind layers, base typography
    ├── App.tsx                       # the shell and its two layouts
    ├── types/api.ts                  # the TypeScript view of the HTTP API
    ├── services/api.ts               # the only module that calls fetch
    ├── hooks/
    │   ├── useHealthCheck.ts         # GET  /api/health
    │   ├── useRunInvestigation.ts    # POST /api/investigate
    │   ├── useInvestigations.ts      # POST /api/investigations + DELETE
    │   └── useInvestigationDetail.ts # POST /api/investigations/{id}
    └── components/
        ├── common/
        │   ├── Header.tsx            # brand + health badge
        │   ├── Spinner.tsx           # the one indeterminate spinner
        │   └── ErrorEnvelope.tsx     # the one way an ApiError is shown
        └── investigation/
            ├── InvestigationForm.tsx
            ├── InvestigationHistoryTable.tsx
            ├── InvestigationDetailView.tsx
            ├── StructuredReportView.tsx
            └── PipelineGraph.tsx
```

The dependency arrow runs one way through that list. Components know about
hooks, hooks know about `services/api.ts`, and **nothing above the service layer
sees a `Response`, a status code or a raw error envelope.** Callers get typed
data or an `ApiError`.

### Component hierarchy

```
App                                     owns useInvestigations + selectedId
├── Header                              owns useHealthCheck
└── main
    ├── FirstLoad                       neither layout yet — see below
    │
    ├── Scenario B  (nothing stored)
    │   ├── InvestigationForm           owns useRunInvestigation
    │   └── EmptyState
    │
    └── Scenario A  (records exist)
        ├── InvestigationForm           sticky on ≥ lg
        ├── InvestigationHistoryTable   owns query + page + confirmingId
        │   └── Row × 10                ScoreBadge, TrashIcon, inline confirm
        └── InvestigationDetailView     owns useInvestigationDetail + tab
            ├── StructuredReportView    tab 1 — five collapsible Panels
            │   ├── AiInsightsPanel         SignatureCard, AnomalyCard
            │   ├── InvestigationNotesPanel
            │   ├── ParserMetricsPanel
            │   ├── MetadataPanel           ConfidenceRing
            │   └── DeterministicOutputsPanel  DistributionBars, TimelineEventRow
            └── PipelineGraph           tab 2 — the React Flow canvas
```

> **On naming.** The brief refers to a `PipelineExecutionGraph` component. The
> file and the exported symbol are `PipelineGraph` / `PipelineGraph.tsx`;
> **Pipeline Execution Graph** is the label of the tab that renders it, defined
> in `InvestigationDetailView.tsx`'s `TABS`. They are the same thing.

### State management

There is no Redux, no Zustand, no React Query and no context provider. State
lives at the lowest node that needs it, and is lifted exactly one level when two
siblings share it:

| State | Owner | Why there |
| --- | --- | --- |
| `selectedId` | `App` | Shared: the table highlights the selected row, the detail view fetches it |
| The record list | `App` (`useInvestigations`) | Shared: the table renders it and the layout branches on its `total` |
| Delete in-flight / error | `App` (`useDeleteInvestigation`) | Sits beside the list rather than inside it, so a consumer reading only `total` does not carry a destructive method |
| Health status | `Header` | Global but read by nothing below — threading it down would make every view accept a prop it never reads |
| Form fields, run result | `InvestigationForm` | The run and the feedback about the run are one event seen from three angles |
| `query`, `page`, `confirmingId` | `InvestigationHistoryTable` | All three are views onto the loaded array and mean nothing outside it |
| Active tab | `InvestigationDetailView` | Deliberately survives a change of `selectedId`, so comparing three runs' pipelines does not mean re-picking the tab each time |
| Panel open/closed | Each `Panel` | Per panel, so remounting the view returns everything to closed |

Four conventions hold across all four hooks:

- **`loading` is derived, never stored.** One piece of state records the request
  that last settled, tagged with a token; a request is in flight exactly when
  that tag no longer matches what the render wants. Storing it would mean a
  synchronous `setLoading(true)` inside the effect, starting a second render
  before the first has painted.
- **Aborts are not errors.** Every service function takes an optional
  `AbortSignal` and rethrows `AbortError` untouched, so a hook can tell "the
  component moved on" from "the request failed".
- **One error type.** A 404, a 422, a 503, an unreachable server and an HTML 502
  from a proxy with no backend behind it all arrive as `ApiError`, with `status: 0`
  reserved for a transport failure that produced no response. `ApiError.isRetryable`
  is true for `0`, `503` and `504`, and that is what decides whether
  `ErrorEnvelope` draws a retry button.
- **`null` is a state, not a gap.** A `null` id in `useInvestigationDetail`
  issues no request and reports neither data nor error — idle is distinct from
  loading, and both are distinct from an empty report.

### The undecided first paint

`App` renders one of three things, and the third is the interesting one:

```tsx
const undecided = history.data === null && history.error === null
const isEmpty   = history.data !== null && history.data.total === 0
```

Which layout renders is decided by one fact — whether any investigation is
stored — and the decision is deferred until that fact is known. A first paint
that guessed would guess wrong half the time and snap from one layout to the
other. An **unreachable** database is not an empty one: that falls through to
Scenario A, where the table's error envelope carries the reason and a retry.

| Condition | Layout |
| --- | --- |
| `undecided` | `FirstLoad` — a spinner, neither layout committed to |
| `isEmpty` | **Scenario B** — the form alone, centred, with an `EmptyState` card |
| otherwise | **Scenario A** — form beside the history on `lg`, inspection panel full-width below both |

### Batch loading and the 1,000-row cap

`useInvestigations` reads the **whole table**, not one page, and the reason is
the search box. The endpoint paginates server-side, but the history panel
filters client-side, and a filter has to see everything it claims to search.
Filtering one page of ten would make "no results" mean "not on the page you
happen to be looking at" — a claim about absence the filter has no standing to
make.

| Constant | Value | Role |
| --- | --- | --- |
| `FETCH_PAGE_SIZE` | `100` | The API's `MAX_LIMIT`; anything above is a 422, so this is the fewest round trips the table can be read in |
| `MAX_RECORDS` | `1000` | Ten requests' worth. The ceiling on what the client holds in memory and re-scans on each keystroke |
| `PAGE_SIZE` | `10` | Rows shown per page of the **filtered** list, in the table component |

The load is a first request awaited alone — it is what reports `total`, and
there is no way to know how many requests are needed without it — followed by
the remaining pages fetched concurrently with `Promise.all`. A 400-row table
therefore costs one round trip plus one, not four.

```ts
const first = await listInvestigations(1, FETCH_PAGE_SIZE, signal)
const capped = Math.min(first.total, MAX_RECORDS)
const pagesNeeded = Math.ceil(capped / FETCH_PAGE_SIZE)
// … pages 2..n concurrently …
return { items: items.slice(0, capped), total, truncated: total > capped }
```

Two details are deliberate. The result is **trimmed rather than trusted**:
`total` can grow between the first request and the last, so the tail page may
carry rows past the cap. And reaching the ceiling sets `truncated` rather than
failing — the table then says so in its header, because a search over a silently
clipped set is the same lie moved somewhere less visible.

### Scroll refs

`App` holds two refs and one helper, and both refs exist because the inspection
panel renders *below* the table:

| Ref | Target | Fires when |
| --- | --- | --- |
| `detailRef` | The `<div>` wrapping `InvestigationDetailView` | A **different** record is selected |
| `historyRef` | The history **column**, not the table body | "Close Inspection", or the inspected record is deleted |

```tsx
useEffect(() => {
  if (selectedId === null) return
  scrollIntoView(detailRef.current)
}, [selectedId])
```

An effect rather than a click handler, because the panel does not exist at click
time: `selectedId` is what mounts it, so scrolling in the handler would aim at a
ref that is still `null`. The dependency is `selectedId` alone, so re-selecting
the same row does not re-scroll and neither does an unrelated re-render — a
refetch landing underneath a reader should not yank the page.

Closing goes the other way, and issues the scroll **before** the state change:
the table sits above the panel, so removing the panel cannot move it and its
position is already correct. Waiting for a re-render would only risk scrolling
to an element mid-relayout.

`scrollIntoView` honours `prefers-reduced-motion`: `behavior: 'smooth'` is the
requested effect and the right default, but for some readers a long smooth
scroll is nauseating rather than pleasant. Those readers still get taken to the
panel — they get taken there instantly.

---

## Key UI Capabilities

### Form submission

`InvestigationForm` owns `useRunInvestigation` and posts one
`InvestigateRequest`.

| Control | Field | Behaviour |
| --- | --- | --- |
| Application name | `application_name` | Required, `maxLength` 255 — the column width the backend enforces |
| Custom ID | `investigation_id` | Optional, `maxLength` 255, monospace. **Omitted entirely when blank** rather than sent as `""` |
| Raw logs | `raw_logs` | Required. Three input routes, one state |
| Analysis mode | `analysis_mode` | Segmented control over `fast` / `standard` / `deep`; `aria-pressed` marks the active tier. Defaults to `standard` |
| LLM provider | `llm_provider` | `<select>` over OpenAI, Anthropic, Gemini, DeepSeek, Local (OpenAI-compatible). Defaults to `local` |
| Enable web search | `enable_web_search` | Checkbox, off by default, styled with `accent-color` rather than a replaced control |

Three details are worth stating because they are not guessable:

- **A blank custom ID is omitted, not sent empty.** The backend treats a
  supplied id as authoritative and never replaces it, so `""` would be a
  *supplied* empty id rather than a request to generate one — and the request
  model would 422 on it. The field's help text also states the consequence of
  reuse, because it is neither guessable nor small: the graph's write is an
  upsert keyed on this value, so re-running with an existing id **overwrites**
  that investigation. That is the feature — it is what makes a re-run correct a
  stored row — and it is also the way to lose a report by accident.
- **Logs reach `raw_logs` three ways**: a dropped file, a picked file, or
  typing. All three write the same state, because the API takes log *text* and
  has no notion of a file. The drop zone accepts `.txt`, `.json` and `.log`,
  matched on the **extension** rather than on `File.type` — browsers report `""`
  for `.log` everywhere and `application/json` only sometimes, so a MIME check
  would reject the project's own `sample_logs/*.log` fixtures. Files are capped
  at 8 MB, which guards the *browser* rather than the pipeline: the text lands
  in a controlled `<textarea>` that repaints on every keystroke, and the
  rejection message points at `POST /api/investigate` for the full-corpus case.
  An **Insert sample logs** button drops in four Pino JSON lines carrying one
  ERROR, so a first run produces a real signature instead of an empty report.
- **The submit button is disabled, not the request rejected.** A blank name or
  blank logs is a 422 server-side; checking `trim()` locally turns that into a
  disabled button rather than a round trip that fails.

The result panel distinguishes two successes. `db_persisted: true` is emerald,
`db_persisted: false` is **amber rather than red** — the analysis ran and every
finding is intact, only the write to PostgreSQL failed. `investigation_notes`
renders underneath in both cases, with any line matching
`/unavailable|could not|failed|warning|skipped|omitted/i` tinted amber. Every
LLM node in this pipeline degrades rather than raises, so a run can come back
complete-looking with its interpretation silently missing; the notes are the
sole evidence of that, which is why they are shown rather than hidden behind a
toggle.

`App.handleCompleted` refetches the history **only** when `db_persisted` is
true. Refetching for an unstored run would redraw the same rows and imply the
record had landed.

### History & search

`InvestigationHistoryTable` receives every loaded row and does two things in a
fixed order — **filter first, paginate second**.

The searchable text of a row is built by `searchableText()`, and the rule is
that the filter matches **what the row displays**:

| Searchable | Not searchable |
| --- | --- |
| `investigation_id` | `created_at` |
| `application_name` | |
| `analysis_mode` | |
| `llm_provider` | |
| `confidence_score`, as its number **or** as `n/a` when it is `null` | |

`n/a` is the interesting one. `confidence_score` is nullable, and `null` means
*not measured* while `0` means *measured as zero* — a distinction the database,
the API, the record list and the confidence ring all preserve. The badge renders
`n/a`, and the filter matches `n/a`, so searching it finds the unmeasured runs.
`created_at` is deliberately excluded: it renders through `toLocaleString`, so
what a row displays depends on the reader's locale and time zone, and matching
the raw ISO string instead would mean matching text that appears nowhere on
screen.

Matching itself is **multi-term AND, case-insensitive, substring**:

```ts
const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
return terms.every((term) => haystack.includes(term))
```

So `openai fast` narrows rather than widens, and a fragment of an id (`abfb`)
matches — users type fragments far more often than whole ids, and a prefix-only
or word-boundary rule would miss exactly that case. There is **no debounce**:
filtering runs over rows already in memory, so there is no request to spare and
a delay would only make the table feel slower than it is. Escape clears the
field, matching what the native `type="search"` affordance does in the browsers
that draw one.

Pagination is over the filtered array, ten rows a page, and the current page is
**clamped on render rather than reset in an effect**:

```ts
const safePage = totalPages === 0 ? 1 : Math.min(page, totalPages)
```

Narrowing the query while on page 4 should land on the last page of what is
left, not silently on page 1 — and deriving it every render means there is no
frame in which the pager and the rows disagree. Changing the query does reset
the cursor to 1, because the old cursor means nothing against a different result
set.

The header reports two counts while filtering (`3 of 47 shown`) and one when not
(`47 stored investigations`), because "3 investigations" over a filtered list
would misreport how much is stored. When the load hit `MAX_RECORDS` *and* a
filter is active, a warning line states the clip — not only when the query
matches nothing, since a query returning 12 rows out of a clipped set is just as
incomplete as one returning none. And "no matches" and "nothing stored" are
rendered as the different facts they are.

### Inline actions

Deletion is confirmed **in the row**, not in a modal. The row is where the
record's identity already is — id, application, score and date are all on screen
and stay on screen — whereas a dialog has to re-state which record it means and
can only quote a fragment of it. It also keeps the destructive click and its
confirmation in one place, and needs no focus trap or escape handling to be
accessible.

| State | What the Actions cell shows |
| --- | --- |
| Idle | The trash button |
| Confirming | **Delete** and **Cancel**, with Cancel `autoFocus`ed so the safe choice is the one already under focus. The row turns red-ringed |
| In flight | A spinner and "Deleting…", the row at 50% opacity with `aria-busy` |
| Another row in flight | This row's trash button is `disabled` |

`confirmingId` is a single id rather than a set, so opening a second
confirmation closes the first and there is never more than one armed destructive
button. Every control in the cell stops propagation, because the row itself is a
button that opens the inspection panel — a click that reached it would select
the very record the reader is trying to delete.

`useDeleteInvestigation` is **single-flight**: `pendingId` is the id in the air
rather than a boolean, so the table disables and spins one row instead of
freezing all of them, and a second delete while one is pending is refused rather
than queued. Two concurrent deletes would produce two refetches racing to
describe the same table. The request is deliberately *not* abortable on
unmount — a delete is not idempotent server-side, so cancelling the client's
interest in the answer would only hide an outcome that already happened.

A failed delete is reported **above** the table rather than in the row, because
the row may well be gone by the time the failure renders. It carries its own
**Refresh list** and **Dismiss** buttons rather than deferring to
`ErrorEnvelope`'s retry affordance, which only appears for statuses it considers
retryable: the most likely failure of a delete is a 404 meaning the record was
already removed elsewhere, which is precisely the case where the list on screen
is provably stale and refreshing is the right move.

Page clamping after a delete needs no special handling. Paging is client-side
over the loaded array and `safePage` is derived on every render, so deleting the
last row of the last page lands on the new last page by itself. `App` clears
`selectedId` **only** when the deleted record is the inspected one — deleting
some other row must not close a panel the reader is reading — and that case also
scrolls back to the list, since the thing being looked at is gone.

### Interactive inspection

Clicking a row mounts `InvestigationDetailView`, which renders four states, not
two: **idle** (`investigationId === null` → nothing at all, so the table below is
undisturbed), **loading**, **error**, and **loaded**. A fifth is worth naming: a
row can exist with a `NULL` report, which the API returns as `{}` rather than a
404, so "the record is there but carries no report" is a real state with its own
message.

Two tabs sit over one payload:

**Tab 1 — Structured Report.** Five collapsible panels, each stating its
*provenance*, because the stored document is partitioned by where a fact came
from rather than by topic and that split is the whole design:

| Panel | Provenance line | Collapsed header carries |
| --- | --- | --- |
| AI Insights | Inference — what three models concluded | signature count, anomaly count, `· synthesis degraded` when the fallback fired |
| Investigation Notes | Runtime record — what each node said about its own limits | `N notes, M flagged` |
| Parser Metrics | Measurement — ingestion health | format, `parsed/total lines`, malformed and unstamped counts when non-zero |
| Metadata | Run identity — the reproducibility record | mode · provider · `score N/100` or `score n/a` |
| Deterministic Outputs | Measurement — arithmetic, reproducible from the same logs | error count, warning count, timeline event count |

Panels are **closed by default and there is no `defaultOpen` prop to pass**. A
whole report expanded at once is several screens of dense output with no way to
see its shape, so the five collapsed headers are the table of contents — which
is why each one carries a summary rather than just a title. The open flag lives
per panel, so remounting the view returns everything to closed. That remount is
forced deliberately: `StructuredReportView` is keyed on
`detail.investigation_id`, because without a key React reconciles by position,
the panels keep their flags, and the next report opens with whichever sections
the *previous* one had been expanded to.

The order is inference first, measurement second — the reverse of how the graph
builds them. A reader opening a stored investigation wants the conclusion and
the caveats on it; the arithmetic it rests on is what they scroll to when they
want to check that conclusion.

Inside the panels, several nested affordances exist for the same reason —
volume:

- **Signature cards** show the masked `template` in monospace with the unmasked
  `sample_messages` behind a Show/Hide toggle. The primary cause is
  red-ringed and badged; `is_root_cause_candidate` is shown as a positive flag
  only, never as "ruled out", because it defaults to `false` when the LLM pass
  degraded.
- **Distribution bars** normalize against the largest row rather than a total,
  because a distribution is capped at its top 20 entries and the counts
  therefore need not sum to the dataset size. A `null` logger renders as an
  italic `(no logger)` chip — in `inv-graph-001` that bucket is 1,514 records,
  so dropping it would silently lose the majority of the dataset, and printing
  `null` would read as a logger named "null".
- **The timeline** renders 20 events with a "Show all N events" toggle.
  Milestones sit on the rail as filled purple markers and buckets as hollow
  ones, so the narrative moments are scannable in a series of hundreds.
- **The confidence ring** is a conic gradient with the middle masked out, tiered
  emerald ≥ 80 / amber ≥ 50 / red below, and renders `n/a` in grey when the
  score is `null` — one more place the not-measured/measured-as-zero distinction
  must not be collapsed.

**Tab 2 — Pipeline Execution Graph.** A React Flow canvas, 420px tall, drawing
the seven stages a stored report carries evidence for.

The framing matters more than the drawing. A stored `structured_report` does not
carry `completed_stages` — that channel lives in graph state and never reaches
the database — so this view cannot replay which nodes ran. It **infers** each
stage's outcome from the two things the report does preserve: whether the
artifact that stage owns is present and intact, and what that stage recorded in
`synthesis.investigation_notes` (attributed by the `"Parser: "`,
`"Error analysis: "`, `"Timeline: "` … prefixes the nodes emit). Every status is
therefore evidence-based rather than reported, and clicking a stage opens an
inspector that names the evidence so a reader can disagree with it.

| Tier | Colour | Meaning |
| --- | --- | --- |
| `ok` | `#34D399` | Ran and published its artifact intact |
| `warning` | `#FBBF24` | Ran and published, but degraded — an LLM pass that fell back to arithmetic, a data-quality caveat, a root cause the model declined to name |
| `error` | `#F87171` | The artifact is missing |

The amber tier is the one that earns its keep: every LLM node degrades rather
than raises, so a degraded run and a healthy one publish an identical shape and
are indistinguishable from the report's structure alone. Each stage's status is
derived from only its own artifact and its own notes, so no wrong guess can
cascade. Edges are coloured by the stage they *leave*, so a degraded stage
visibly taints everything downstream of it; the four dashed edges are the ones
that are conditional or bypassable at runtime — notably
`error_analysis → prepare_output`, which in the real graph may first detour
through `web_search`.

Two absences are deliberate and stated in the UI itself. **`web_search` is not
drawn**: it writes no report section, so a stored report carries no evidence of
whether it ran, and drawing it would mean drawing a status with nothing behind
it. And **`write_to_db` is not inferred at all** — the report is being read back
*out of* the table, so the write demonstrably succeeded. No other stage has
evidence that direct.

The canvas is an inspector rather than an editor: panning, zooming and the
controls are available because they help on a narrow screen, but
`nodesDraggable`, `nodesConnectable` and `edgesFocusable` are all off, since
dragging a node would only let a reader misrepresent a topology that is fixed by
`graph.py`.

### Status badges

| Badge | Where | States |
| --- | --- | --- |
| Health | `Header`, `role="status" aria-live="polite"` | **Checking…** (blue, pulsing), **System Online** (emerald), **Backend Offline** (red) |
| Score | Each history row, `ScoreBadge` | emerald ≥ 80, amber ≥ 50, red below, grey `n/a` when unmeasured |
| Run result | Below the form | emerald *Investigation stored*, amber *Analysis ran, not stored* |
| Stage | Each node on the pipeline canvas | *Completed* / *Degraded* / *Artifact missing* |

The health badge distinguishes three states rather than two on purpose:
reporting "Backend Offline" before the first request has answered would tell the
user the system is down every single time the page loads. Once settled, a later
refetch keeps showing the last known state rather than flickering back to
"checking".

---

## Local Setup & Development Commands

### Prerequisites

The frontend needs Node.js (20+; developed against v26) and npm. It also needs
something to talk to — see [`BACKEND_README.md`](BACKEND_README.md#environment-setup--execution)
for the full setup, but the short form is three terminals:

```bash
# terminal 1 — optional: the offline mock LLM, for llm_provider: Local
python3 -m uvicorn tests.mock_local_llm:app --port 8000

# terminal 2 — the API on 127.0.0.1:8010
python3 backend.py

# terminal 3 — this app on localhost:5173
cd frontend && npm run dev
```

The `investigations` table must exist (`python3 init_db.py`, once) or the three
storage endpoints answer `503` and the history panel renders its error envelope
instead of a list.

### Commands

| Command | What it does |
| --- | --- |
| `npm install` | Install dependencies from `package-lock.json` |
| `npm run dev` | Vite dev server on `http://localhost:5173` with HMR and the `/api` proxy |
| `npm run build` | `tsc -b` (type-check, project references) then `vite build` into `dist/` |
| `npm run lint` | `oxlint` over the workspace using `.oxlintrc.json` |
| `npm run preview` | Serve the built `dist/` locally — **note this has no `/api` proxy**, so point `VITE_API_BASE_URL` at the backend origin or put a reverse proxy in front |

```bash
cd frontend
npm install
npm run dev
```

`npm run build` is a real type-check, not just a bundle: `tsc -b` runs first and
a type error fails the build before Vite is invoked.

### Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `/api` | Where the client sends requests. Relative by default, which is what makes the dev proxy work and what makes one reverse proxy work in production |

---

## Frontend Step 6 Test Results Matrix

Fifteen end-to-end verification runs executed manually through the browser
against the live stack — `tests/mock_local_llm.py` on `:8000` (for the `Local`
provider), `python3 backend.py` on `:8010`, and the Vite dev server on `:5173` —
with a live PostgreSQL behind the API.

Every run below is a **UI** run: the payload was entered in
`InvestigationForm` and the outcome read from the run-result panel and the
history table, not from Postman or curl.

> The database state evolves across the sequence. Test 1 empties the table
> through inline row deletion, Tests 2–3 insert the first two records, Test 4
> adds ten more, and Tests 5–15 add one each. Run them in order to reproduce the
> row counts.

### Summary

| # | Scenario | Application name | Investigation ID | Mode | Provider | Web search | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Empty state handling | — | — | — | — | — | ✅ Pass |
| 2 | Custom ID & auto-scroll | `db first record` | `inv-graph-001` | fast | Local | on | ✅ Pass |
| 3 | Auto-generated ID & deep mode | `db second record` | *generated* | deep | Local | off | ✅ Pass |
| 4 | Search & pagination at scale | 10 records via UI | *various* | — | — | — | ✅ Pass |
| 5 | OpenAI fast + web search | `open ai test 1` | *generated* | fast | OpenAI | on | ✅ Pass |
| 6 | OpenAI standard + custom ID | `open ai test 2` | `ing-graph-014` | standard | OpenAI | off | ✅ Pass |
| 7 | OpenAI deep mode | `open ai test 3` | `ing-graph-015` | deep | OpenAI | off | ✅ Pass |
| 8 | Anthropic fast + web search | `anthropic test 1` | *generated* | fast | Anthropic | on | ✅ Pass |
| 9 | Anthropic standard + web search | `anthropic test 2` | *generated* | standard | Anthropic | on | ✅ Pass |
| 10 | Anthropic deep mode | `anthropic test 3` | *generated* | deep | Anthropic | off | ✅ Pass |
| 11 | Gemini standard + web search + custom ID | `gemini test 2` | `ing-graph-020` | standard | Gemini | on | ✅ Pass |
| 12 | Gemini deep mode | `gemini test 3` | *generated* | deep | Gemini | off | ✅ Pass |
| 13 | DeepSeek fast mode | `deepseek test 1` | *generated* | fast | DeepSeek | off | ✅ Pass |
| 14 | DeepSeek standard + web search | `deepseek test 2` | *generated* | standard | DeepSeek | on | ✅ Pass |
| 15 | DeepSeek deep mode | `deepseek test 3` | *generated* | deep | DeepSeek | off | ✅ Pass |

### Payloads used

| Label | Source |
| --- | --- |
| **JSON snippet** | A four-line Pino JSON payload — `POST /bookings`, payment provider slow, payment provider unavailable, request completed with status `503`. The same shape the form's *Insert sample logs* button produces |
| `fastapi_recovery.log` | `sample_logs/fastapi_recovery.log` |
| `typescript_pino_recovery.log` | `sample_logs/typescript_pino_recovery.log` |
| `java_spring_boot_large.json.log` | `sample_logs/java_spring_boot_large.json.log` |
| `java_spring_boot_large.text.log` | `sample_logs/java_spring_boot_large.text.log` |

### Test 1 — Empty state handling

Cleared every stored record using inline row deletion, one row at a time,
confirming in-row each time. Once the database count reached zero, the layout
switched to **Scenario B**: only the *New Investigation* panel rendered,
centred, beside the "No investigations stored yet" empty-state card. The history
table, its search box and its pager were all absent rather than rendered empty.

This is the `isEmpty` branch of `App`, and it exercised page clamping on the way
down: deleting the last row of a page moved the pager to the new last page
without a reset to page 1.

### Test 2 — Custom ID & auto-scroll

| Input | Value |
| --- | --- |
| `application_name` | `db first record` |
| `investigation_id` | `inv-graph-001` |
| `raw_logs` | JSON snippet (POST /bookings, payment slow/unavailable, status 503) |
| `analysis_mode` | `fast` |
| `llm_provider` | `Local` |
| `enable_web_search` | `true` |

**Result:** the record was inserted under the supplied id `inv-graph-001` —
confirmed by the run-result panel echoing it and the history row appearing under
that key rather than a generated one. With a record now stored, the layout
switched from Scenario B to Scenario A.

Both scroll behaviours were verified on this record: selecting the row scrolled
**down** to the inspection panel, and *Close Inspection* scrolled **up** to the
top of the history card.

### Test 3 — Auto-generated ID & deep mode

| Input | Value |
| --- | --- |
| `application_name` | `db second record` |
| `investigation_id` | *blank — generated by the backend* |
| `analysis_mode` | `deep` |
| `llm_provider` | `Local` |
| `enable_web_search` | `false` |

**Result:** inserted successfully under a backend-generated
`inv-graph-<4 hex>` id, reported in the run-result panel. Both auto-scroll
directions verified again on the generated-id row.

### Test 4 — Search & pagination at scale

Inserted 10 additional records through the UI, bringing the table to 12. With
more than one page of rows:

- Real-time client-side filtering was verified on every keystroke with no
  debounce lag, including **multi-term** queries where every term must match
  (`local deep`, and id fragments).
- The header switched from `12 stored investigations` to `N of 12 shown` while
  filtering.
- Page switching was verified in both directions, and narrowing a query while on
  a later page clamped to the last page of the remaining results rather than
  resetting to page 1.

### Tests 5–7 — OpenAI

| # | Application name | Logs | Custom ID | Mode | Web search | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 5 | `open ai test 1` | `fastapi_recovery.log` | — | fast | on | Success |
| 6 | `open ai test 2` | `fastapi_recovery.log` | `ing-graph-014` | standard | off | Success |
| 7 | `open ai test 3` | `typescript_pino_recovery.log` | `ing-graph-015` | deep | off | Success |

All three stored, appeared in the history table on refetch, and rendered both
tabs of the inspection panel.

### Tests 8–10 — Anthropic

| # | Application name | Logs | Mode | Web search | Result |
| --- | --- | --- | --- | --- | --- |
| 8 | `anthropic test 1` | `fastapi_recovery.log` | fast | on | Success |
| 9 | `anthropic test 2` | `typescript_pino_recovery.log` | standard | on | Success |
| 10 | `anthropic test 3` | JSON snippet | deep | off | Success |

### Tests 11–12 — Gemini

| # | Application name | Logs | Custom ID | Mode | Web search | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 11 | `gemini test 2` | `java_spring_boot_large.json.log` | `ing-graph-020` | standard | on | Success |
| 12 | `gemini test 3` | `typescript_pino_recovery.log` | — | deep | off | Success |

Test 11 is also the largest payload in the sequence, and is what exercised the
form's 8 MB file ceiling being comfortably clear of a real benchmark dataset.

### Tests 13–15 — DeepSeek

| # | Application name | Logs | Mode | Web search | Result |
| --- | --- | --- | --- | --- | --- |
| 13 | `deepseek test 1` | `java_spring_boot_large.text.log` | fast | off | Success |
| 14 | `deepseek test 2` | `java_spring_boot_large.json.log` | standard | on | Success |
| 15 | `deepseek test 3` | JSON snippet | deep | off | Success |

### Coverage read off the matrix

| Provider | fast | standard | deep |
| --- | --- | --- | --- |
| Local | ✅ Test 2 | — | ✅ Test 3 |
| OpenAI | ✅ Test 5 | ✅ Test 6 | ✅ Test 7 |
| Anthropic | ✅ Test 8 | ✅ Test 9 | ✅ Test 10 |
| Gemini | — | ✅ Test 11 | ✅ Test 12 |
| DeepSeek | ✅ Test 13 | ✅ Test 14 | ✅ Test 15 |

Two cells of the provider × mode grid were not exercised in this round — **Gemini
fast** and **Local standard**. They are recorded as gaps rather than left to be
inferred from the ✅s around them. Web search was exercised on 6 of the 15 runs
(Tests 2, 5, 8, 9, 11, 14), across four of the five providers, and custom ids on
4 (Tests 2, 6, 7, 11).

---

## Verification

Both checks were run from `frontend/` against the state of the tree this
document describes.

```bash
$ npm run lint
> oxlint
# exit 0, no diagnostics
```

```bash
$ npm run build
> tsc -b && vite build

vite v8.2.2 building client environment for production...
✓ 184 modules transformed.
dist/index.html                   0.46 kB │ gzip:   0.30 kB
dist/assets/index-BdHKXWOR.css   36.25 kB │ gzip:   7.30 kB
dist/assets/index-CsJuYSv7.js   441.13 kB │ gzip: 135.08 kB
✓ built in 318ms
# exit 0
```

`oxlint` 1.81.0 reports zero errors and zero warnings, and `tsc -b --force`
completes with no diagnostics — so the type-check is genuinely clean rather than
served from the build-info cache.

One note on the bundle: `dist/assets/index-*.js` is 441 kB raw / 135 kB gzipped,
and most of that is `@xyflow/react`, which is loaded eagerly. That is a
deliberate non-issue for a locally served development tool, but it is the first
thing to code-split (behind the Pipeline Execution Graph tab) if this app is
ever served over a network where the first paint matters.

# LogSherlock Backend

The HTTP layer between a React client and the LogSherlock analysis graph. It
runs investigations through the LangGraph pipeline, then lists, reads and
deletes the reports that pipeline stored in PostgreSQL.

This document covers the backend only. The graph itself — its eight nodes,
their state contracts, algorithms and degradation guarantees — is documented in
[`GRAPH_README.md`](GRAPH_README.md), and nothing here duplicates it.

---

## Contents

- [System Overview & Architecture](#system-overview--architecture)
- [Environment Setup & Execution](#environment-setup--execution)
- [API Reference & Contracts](#api-reference--contracts)
- [Manual Postman Test Suite Verification](#manual-postman-test-suite-verification)
- [Automated Testing Strategy](#automated-testing-strategy)
- [Operational Notes](#operational-notes)

---

## System Overview & Architecture

### Purpose

The backend exists to bridge three things that otherwise cannot talk to each
other:

- **The React UI**, which speaks JSON over HTTP and needs two very differently
  shaped payloads — a light list to paint on load, and one heavy report when a
  user clicks a row.
- **The LangGraph engine** in `graph.py`, which is a Python function taking a
  state dict and returning a state dict, with no network surface of its own.
- **PostgreSQL**, where the graph's own `write_to_db` node persists each
  completed investigation.

Its responsibilities are deliberately narrow. It validates incoming payloads,
invokes the compiled graph, reads what the graph stored, and translates failures
into status codes. **It contributes no analysis and defines no shared model.**
`graph.py` is imported and called but never modified; `graph_library.models`
supplies the report shape; `graph_library.write_to_db` supplies the table name
and the connection settings. A second opinion in this layer about what a report
looks like, or which LLM providers exist, would be a second source of truth.

### Where it sits

```
   React client (localhost:3000 / :5173)
            │  JSON over HTTP, CORS-allowed
            ▼
   ┌─────────────────────────────────────────────┐
   │  FastAPI application  (backend/)            │
   │                                             │
   │   routes  ──►  services  ──►  repository    │
   │                    │                        │
   │                    └──►  graph factory      │
   └─────────────────────────────────────────────┘
            │                        │
            │  compile & invoke      │  SELECT / DELETE
            ▼                        ▼
      graph.py  ──► write_to_db ──► PostgreSQL
                     (INSERT)       investigations
```

Note the asymmetry, because it is the single most important thing about this
design: **the API never writes a row.** The only `INSERT` in the system belongs
to the graph's `write_to_db` node. `POST /api/investigate` invokes the graph and
reports where the result went; the three storage endpoints only read and delete.
That is what keeps one statement responsible for creating an investigation
record, and why `InvestigationRepository` has no `create` method.

### Core stack

| Component | Role |
| --- | --- |
| **FastAPI** | Routing, dependency injection, OpenAPI schema generation |
| **Pydantic v2** | Request validation and response serialization at the boundary |
| **Uvicorn** | ASGI server, configured for long-running graph invocations |
| **LangGraph** | The analysis engine, imported from `graph.py` and compiled once |
| **psycopg2** | PostgreSQL driver, imported lazily and only where storage is touched |
| **Pytest** | 68 offline tests over the whole HTTP surface |

FastAPI, Uvicorn and psycopg2 are already core dependencies of the project;
`httpx`, which `fastapi.testclient.TestClient` requires, is in the `dev` extra.
No new dependency was added for the backend.

### Package layout

```
backend.py                        # root entry point — `python3 backend.py`
backend/
├── __init__.py                   # public surface
├── app.py                        # create_app(): the application factory
├── config.py                     # ApiSettings, and the one DB-config accessor
├── schemas.py                    # the Pydantic v2 request/response contract
├── errors.py                     # the error taxonomy and its JSON handlers
├── dependencies.py               # the FastAPI Depends wiring
├── factories.py                  # graph, repository and service factories
├── persistence/
│   ├── queries.py                # every SQL statement the API issues
│   ├── connection.py             # connection handling
│   └── repository.py             # InvestigationRepository + Postgres impl
├── routes/
│   ├── health.py                 # GET /api/health
│   └── investigations.py         # the other four endpoints
└── services/
    ├── investigations.py         # InvestigationService + default impl
    └── graph_runner.py           # GraphRunnerService + LangGraph impl
```

The dependency arrow runs one way through that list: routes know about services,
services know about repositories and factories, and **nothing below the route
layer imports FastAPI.** A service raises a `BackendError` and returns a
Pydantic model; the route layer is the only thing that knows what a status code
is. That boundary is what makes the same service callable from a script or a
scheduled job with no request in flight.

#### A note on the name collision

`backend.py` and the `backend/` package share a name, and Python resolves that
in the package's favour — `import backend` always finds the package. The
collision is harmless because `backend.py` is only ever *executed*, where it is
`__main__`, and never imported. For the same reason it is deliberately **not**
listed in `pyproject.toml`'s `py-modules`: installing both would put a module
and a package of the same name into one `site-packages` directory.

---

### Design Patterns & Engineering Principles

Every abstract interface below has exactly one production implementation today.
The abstractions exist anyway, and they earn their keep for a reason that has
nothing to do with swapping vendors: they are what make the application
testable by *substitution* rather than by patching, and what keep construction
off the request path.

#### Factory Pattern

Three factories, at three levels.

| Factory | Interface | Responsibility |
| --- | --- | --- |
| `CompiledGraphFactory` | `GraphFactory` | Compiles `graph.compile_graph()` once, lazily, and caches it |
| `PostgresRepositoryFactory` | `RepositoryFactory` | Builds repositories against one resolved `DatabaseConfig` |
| `DefaultServiceFactory` | `ServiceFactory` | Assembles the services out of the two above |
| `create_app()` | — | The application factory itself |

`CompiledGraphFactory` is the one that carries real weight. Compilation walks
the whole node registry and is not free, but more importantly it is *pure*: the
compiled graph holds no per-run state, because `compile_graph()` deliberately
attaches no checkpointer — LogSherlock treats each investigation as a stateless
request. One instance therefore safely serves every concurrent invocation. Two
details are load-bearing:

- **The `graph` import happens inside `get_graph()`, not at module scope.** That
  import pulls in every feature package and their dependencies, pandas among
  them, and doing it eagerly would make `import backend` pay for the engine even
  when the caller only wanted the schemas.
- **A double-checked lock guards the first compilation.** Two requests can
  arrive before the first has finished compiling; the lock makes the second wait
  rather than build a second graph and discard one.

`create_app()` is a function rather than a module-level `app = FastAPI()` for
two reasons that both matter: a test builds an application wired to stubs by
passing one argument, and nothing is constructed at import time — so importing
the package reads no environment, resolves no database and compiles no graph.

#### Dependency Injection

Route handlers declare what they need as a parameter, and FastAPI resolves it
through `Depends`. No handler constructs a service, reads the environment or
opens a connection, which is what keeps a route function three lines long.

The chain has exactly one root:

```python
create_app(settings, service_factory)
    └── app.state.service_factory = service_factory
            │
            ▼
        get_service_factory(request)          # reads app.state
            ├── get_investigation_service()   # → InvestigationService
            └── get_graph_runner_service()    # → GraphRunnerService
```

Overriding that single dependency replaces the graph, the repository and both
services at once.

**`app.state` rather than a module-level global, deliberately.** A global would
be shared by every application object in the process, so two `TestClient`
instances in one session — one with a stub graph that succeeds, one with a stub
that raises — would silently share whichever was wired last.

Route signatures read as the contracts they are, using named annotation
aliases:

```python
def list_investigations(
    service: InvestigationServiceDep,
    params: PaginationParams | None = Body(default=None),
) -> PaginatedInvestigationsResponse:
    page = params or PaginationParams()
    return service.fetch(page=page.page, limit=page.limit)
```

#### Abstract Base Classes

| ABC | Abstract methods | Implementation |
| --- | --- | --- |
| `InvestigationRepository` | `fetch_page`, `fetch_one`, `delete` | `PostgresInvestigationRepository` |
| `InvestigationService` | `fetch`, `remove` | `DefaultInvestigationService` |
| `GraphRunnerService` | `execute` | `LangGraphRunnerService` |
| `GraphFactory` | `get_graph` | `CompiledGraphFactory` |
| `RepositoryFactory` | `create_investigation_repository` | `PostgresRepositoryFactory` |
| `ServiceFactory` | `create_investigation_service`, `create_graph_runner_service` | `DefaultServiceFactory` |

Each is a genuine `abc.ABC`, so a partial implementation fails at construction
rather than at the first call — a property the test suite asserts directly for
all six.

The interfaces are shaped by two rules:

- **The repository speaks in rows, not in responses.** It returns `TypedDict`s
  (`InvestigationMetadataRow`, `StoredInvestigation`, `InvestigationPage`). A
  repository that returned `PaginatedInvestigationsResponse` would be a
  repository that knows about HTTP, and the wire format would then be dictated
  by the shape of a `SELECT`.
- **Driver exceptions are translated exactly once, at the repository
  boundary.** Every `psycopg2` failure becomes a `RepositoryError`, so no layer
  above ever imports the driver and swapping the storage engine changes no
  `except` clause upstream.

#### Method Overriding — `@override`

Every concrete implementation marks its overrides with `typing.override`
(Python 3.12+, which this project already requires):

```python
class PostgresInvestigationRepository(InvestigationRepository):
    @override
    def fetch_page(self, *, limit: int, offset: int) -> InvestigationPage:
        ...
```

The decorator makes a renamed or mistyped abstract method a type-checker error
rather than a silently unused method sitting beside an ABC that still refuses to
instantiate.

#### Method Overloading — `typing.overload`

Applied in the two places where one operation genuinely has two call shapes.

**1. `InvestigationService.fetch` — by id, or by page.**

```python
@overload
def fetch(self, investigation_id: str, /) -> InvestigationDetailResponse: ...
@overload
def fetch(self, /, *, page: int = ..., limit: int = ...) -> PaginatedInvestigationsResponse: ...
```

Both answer "give me stored investigations" and differ only in how the caller
identifies what it wants. The overload is what lets one name carry both while a
type checker still knows that passing an id yields a detail response and passing
a page yields a paginated one — a single `fetch` returning a union would push
that discrimination onto every caller. Supplying both raises `TypeError`,
because the two are alternatives rather than a combination.

**2. `GraphRunnerService.execute` — a request model, or loose fields.**

```python
@overload
async def execute(self, request: InvestigateRequest, /) -> InvestigateResponse: ...
@overload
async def execute(self, /, *, application_name: str, raw_logs: str,
                  analysis_mode: str = ..., llm_provider: str = ...,
                  enable_web_search: bool = ...,
                  investigation_id: str | None = ...) -> InvestigateResponse: ...
```

A route handler already holds a validated `InvestigateRequest` and passes it
whole; a script, a test or a future scheduled job holds loose values and should
not have to import a request model to run the pipeline. **The keyword form
validates through the same model**, so neither path can skip a constraint the
other enforces — passing `raw_logs=""` to the keyword form raises exactly as the
HTTP path 422s.

---

## Environment Setup & Execution

### Prerequisites

1. **Python 3.12+** and the project installed from source:

   ```bash
   pip install -e ".[dev]"
   ```

   The backend adds no dependency of its own. Install the extras for whichever
   LLM providers you intend to call — `.[openai]`, `.[anthropic]`, `.[gemini]`,
   `.[search]` — exactly as the graph documentation describes.

2. **A running PostgreSQL** reachable with the `DB_*` credentials below.

3. **A `.env` file** at the repository root. Copy `.env.example` and fill in
   what you need:

   ```bash
   cp .env.example .env
   ```

4. **The `investigations` table**, created once before the first run:

   ```bash
   python3 init_db.py
   ```

   > **This script truncates the table if it already exists.** That is its
   > purpose — it prepares a clean slate — but do not point it at a database
   > whose contents matter. Skipping it entirely is the more common mistake: the
   > three storage endpoints will report a `503` against a table that does not
   > exist yet.

### Port allocations

| Service | Address | Notes |
| --- | --- | --- |
| **FastAPI backend** | `http://127.0.0.1:8010` | Override with `API_PORT` |
| **Mock LLM server** | `http://localhost:8080` | Optional — `tests/mock_local_llm.py`, for offline runs |
| **PostgreSQL** | `localhost:5432` | Override with `DB_HOST` / `DB_PORT` |
| React (CRA) | `http://localhost:3000` | CORS-allowed origin |
| React (Vite) | `http://localhost:5173` | CORS-allowed origin |

**Why 8010 and not 8000 or 8080.** Both of those are already spoken for on the
local-LLM path: `8000` is `llm_factory.DEFAULT_LOCAL_BASE_URL`, and `8080` is
where `GRAPH_README.md` has you start the mock provider. Running the API
alongside a mock LLM is the normal offline setup, so a default that fought
either of them for a socket would be a default that fails on first use.

The backend binds **loopback**, not `0.0.0.0`. This process holds a database
credential and reaches LLM providers with your keys; a development default that
binds every interface is one misconfigured firewall away from being public. Set
`API_HOST=0.0.0.0` deliberately if you need it.

### Configuration

Every variable is optional and every default is a working local value, so the
server starts with no configuration at all.

**Server settings**, read by `backend/config.py`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `API_HOST` | `127.0.0.1` | Bind address |
| `API_PORT` | `8010` | Bind port |
| `API_CORS_ORIGINS` | CRA + Vite, both loopback spellings | Comma-separated allowed origins |
| `API_KEEP_ALIVE_TIMEOUT` | `75` | Seconds an idle connection is held open |
| `API_GRAPH_TIMEOUT` | `900` | Seconds one graph run may take; `0` disables |
| `API_LOG_LEVEL` | `info` | Uvicorn log level |
| `API_RELOAD` | off | Auto-reload on file change (development only) |

**Database settings**, read by `graph_library.write_to_db` and shared verbatim
with `init_db.py` and the `write_to_db` node — the API cannot drift onto a
different database from the one the graph writes to:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DB_HOST` | `localhost` | Server hostname |
| `DB_PORT` | `5432` | Server port |
| `DB_NAME` | `postgres` | Database holding the `investigations` table |
| `DB_USER` | `postgres` | Role to authenticate as |
| `DB_PASSWORD` | *(none)* | Empty is omitted from the connection, not sent as `""` |
| `DB_CONNECT_TIMEOUT` | `5` | Seconds to wait for a connection |

**`.env` is loaded by `backend.py` and by no module inside `backend/`.** That is
the rule the whole project holds: `load_dotenv` mutates `os.environ` for the
entire process, so a library that calls it injects every key in the file —
provider credentials included — into a process that deliberately did not set
them. Populating the environment is an entry point's job.

### Starting the backend

```bash
python3 backend.py
```

```
LogSherlock API starting on http://127.0.0.1:8010
  Interactive docs: http://127.0.0.1:8010/docs
  Health check:     http://127.0.0.1:8010/api/health
  Allowed origins:  http://localhost:3000, http://127.0.0.1:3000, http://localhost:5173, http://127.0.0.1:5173

INFO  backend.factories: Repositories will read from localhost:5432/postgres
INFO  backend.app: LogSherlock API 0.1.0 starting on 127.0.0.1:8010 (graph timeout: 900s)
INFO  backend.app: Investigations database: localhost:5432/postgres
INFO  Uvicorn running on http://127.0.0.1:8010 (Press CTRL+C to quit)
```

The `Investigations database:` line is the single most useful thing in that log
when a deployment turns out to be serving an empty list from the wrong server.
It carries no credential — the label is built from a `host:port/dbname` property
that has no representation for the password.

Interactive OpenAPI documentation is served at `/docs` (Swagger UI) and
`/redoc`.

#### Fully offline runs

To exercise the whole pipeline with no provider keys and no network, start the
project's mock LLM in a second terminal and send `llm_provider: "local"`:

```bash
# terminal 1
python3 -m uvicorn tests.mock_local_llm:app --port 8080

# terminal 2
python3 backend.py
```

`LOCAL_LLM_BASE_URL` in `.env` must name the same port. All four structured LLM
calls — the two error-analysis passes, pattern analysis and the prepare-output
synthesis — are then answered locally.

---

## API Reference & Contracts

All endpoints are mounted under `/api`. Every request and response body is JSON.

### Conventions

**One error envelope.** Every failure, at every status code, returns
`{"detail": ...}` — matching FastAPI's own `HTTPException` shape, so a client
reads one field on a 404, a 422 and a 500 alike rather than branching on the
status code to find out where the message is. `detail` is a string for
deliberate failures and the structured Pydantic error list for a 422.

| Status | Meaning in this API |
| --- | --- |
| `200` | Success |
| `404` | No investigation exists with that id |
| `422` | The request body or path parameter failed validation |
| `500` | The analysis pipeline could not be built or run |
| `503` | The investigations database could not be reached |
| `504` | The analysis exceeded `API_GRAPH_TIMEOUT` |

`503` rather than `500` for a database failure is deliberate: `500` tells a
caller the request is broken and retrying is pointless, `503` tells it the
dependency is down and retrying later is exactly right. An unreachable Postgres
is the second. `504` for a timeout says the same thing about a payload that may
well succeed against a faster provider.

**Unknown fields are rejected.** Every request model sets `extra="forbid"`, so a
client-side typo such as `analysis_modes` is a 422 naming the key rather than a
silently ignored field and a run that used defaults the caller did not intend.

**Two reads are POSTs.** `POST /api/investigations` and
`POST /api/investigations/{id}` are reads and would conventionally be GETs. They
are POSTs because the client contract specifies a JSON request body for each,
and a GET with a body is unspecified territory that proxies and browser `fetch`
implementations handle inconsistently.

**CORS.** Configured for the React development origins in the table above, with
`allow_credentials=True`, methods `GET, POST, DELETE, OPTIONS`, all headers, and
a 600-second preflight cache. The origin list is explicit rather than `["*"]`
because a wildcard and `allow_credentials=True` are mutually exclusive per the
CORS specification — the browser rejects the combination outright, so a wildcard
would break exactly the credentialed requests it appears to permit.

---

### `GET /api/health`

System health check.

**Request:** no parameters, no body.

**Response `200`:**

```json
{
  "status": "ok",
  "message": "Backend is running"
}
```

Deliberately the only endpoint that touches nothing. It does not query the
database and does not compile the graph, because a health check that fails when
Postgres is down would take the whole API out of a load balancer over a
dependency three of its five endpoints do not need — and because a check that
opens a connection is a check that can hang.

---

### `POST /api/investigate`

Synchronous graph invocation and database persistence trigger. Runs the entire
pipeline: parse, statistics, timeline, pattern analysis, error analysis with an
optional web-search detour, synthesis and persistence.

**Request body — `InvestigateRequest`:**

| Field | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `application_name` | `str` | yes | — | Non-blank, ≤ 255 characters |
| `raw_logs` | `str` | yes | — | The log text itself, not a path. Non-blank |
| `analysis_mode` | `str` | no | `"standard"` | `fast`, `standard` or `deep` |
| `llm_provider` | `str` | no | `"openai"` | `openai`, `anthropic`, `gemini`, `deepseek`, `local` |
| `enable_web_search` | `bool` | no | `false` | Opt in to the error-analysis search detour |
| `investigation_id` | `str \| null` | no | *generated* | ≤ 255 characters. Never replaced when supplied |

**Response `200` — `InvestigateResponse`:**

| Field | Type | Meaning |
| --- | --- | --- |
| `investigation_id` | `str` | The key this run was stored under |
| `db_persisted` | `bool` | Whether the report actually reached PostgreSQL |
| `investigation_notes` | `list[str]` | What every node recorded about its own limits |

**Other statuses:** `422` (invalid body), `500` (pipeline could not be built or
run), `504` (exceeded `API_GRAPH_TIMEOUT`).

#### Why `analysis_mode` and `llm_provider` are plain strings

They are not constrained to a `Literal` here on purpose. The graph normalizes
them itself — `normalize_provider` resolves `Claude`, `google`, `GPT` and a
table of observed misspellings — and a stricter copy in this layer would reject
inputs the engine explicitly supports, making the API the thing that decides
which providers exist. An unrecognized provider surfaces as a degraded run with
an explanatory note rather than as a rejected request.

#### `db_persisted: false` is still a `200`

This is the field the UI branches on, and the distinction matters. The analysis
*ran*; only its storage failed. Every node in the graph degrades rather than
raises, so an unreachable provider costs an investigation its interpretation
rather than costing the caller a 5xx. `investigation_notes` carries the reason,
which is what a client puts in its warning toast — a `false` with no explanation
would give the UI nothing to display. Only an exception escaping the graph
entirely is a `500`.

#### Generated identifiers

When `investigation_id` is omitted, one is minted in the form
`inv-graph-<4 hex chars>` from `uuid.uuid4()`, and the response reports it —
which is the only place it appears, and what lets the client fetch back what it
just created. Generating rather than refusing is what makes persistence the
default: a UI with no id field would otherwise produce complete investigations
that are never stored.

> **Caveat, stated plainly.** Four hex characters is a 65,536-value keyspace, and
> because the graph's write is an idempotent upsert keyed on this id, a collision
> **overwrites** the earlier investigation rather than failing. At a few hundred
> stored records the birthday bound makes one likely. Supply your own
> `investigation_id` for anything that must be durable — a supplied id is never
> replaced.

#### Duration and concurrency

This endpoint can hold a connection open for minutes; the payload size is the
caller's choice, and a multi-megabyte corpus legitimately takes that long. Two
things make that safe:

- The handler is `async def` and awaits `graph.ainvoke(...)`, so LangGraph runs
  the synchronous node functions on worker threads while the event loop stays
  free. A blocking `invoke` here would stall every other request on the process.
- `timeout_keep_alive` is raised to 75 seconds, well above uvicorn's default of
  5. Note this bounds the *idle* period between requests, not a handler's
  runtime — uvicorn imposes no ceiling on the latter, which is exactly what a
  long analysis needs. The per-run deadline is the API's own
  `API_GRAPH_TIMEOUT`.

---

### `POST /api/investigations`

Paginated metadata fetch. **`structured_report` is excluded for efficiency** —
and excluded at the SQL projection, not merely omitted from the response model,
so it cannot be pulled over the wire by accident.

**Request body — `PaginationParams`** (an omitted body is equivalent to `{}`):

| Field | Type | Default | Constraints |
| --- | --- | --- | --- |
| `page` | `int` | `1` | `≥ 1` |
| `limit` | `int` | `10` | `1 … 100` |

The ceiling on `limit` exists because it sizes a database round trip the caller
controls: without it, `limit=100000` is a denial of service written in JSON.

**Response `200` — `PaginatedInvestigationsResponse`:**

| Field | Type | Meaning |
| --- | --- | --- |
| `items` | `list[InvestigationMetadataItem]` | The rows on this page, newest first |
| `total` | `int` | Rows in the whole table, not on this page |
| `page` | `int` | The page that was served |
| `limit` | `int` | The page size that was served |
| `total_pages` | `int` | `ceil(total / limit)`, and `0` for an empty table |

Each item carries `investigation_id`, `application_name`, `confidence_score`,
`analysis_mode`, `llm_provider`, `created_at` and `updated_at` — nothing else.

**Other statuses:** `422` (invalid pagination), `503` (database unreachable).

Three behaviours worth knowing:

- **Ordering is `created_at DESC NULLS LAST, investigation_id ASC`.** The
  tiebreaker is load-bearing rather than tidy: `created_at` shares one
  `CURRENT_TIMESTAMP` per transaction, so rows written together tie, and an
  unstable sort under `LIMIT`/`OFFSET` lets the same row appear on two pages
  while another appears on none.
- **`total_pages` is `0` for an empty table**, not `1`, so a client can test the
  field directly instead of special-casing "one page that happens to contain
  nothing".
- **A page past the end returns empty `items` with the real `total`**, which is
  what lets a client recover from asking for a page that no longer exists. The
  count and the rows come from the same transaction, so the pager always agrees
  with the rows beside it.

`confidence_score` may be `null`. `null` means "not measured" and `0` means
"measured as zero"; the distinction survives from the column all the way to the
client and is never collapsed.

---

### `POST /api/investigations/{id}`

Full report lookup, returning the complete `structured_report` JSONB document.

**Path parameter:** `id` — 1 to 255 characters.

**Request body:** empty. `{}` and an omitted body both validate. The model is
declared with no fields so that a future filter is an added field rather than a
changed signature.

**Response `200` — `InvestigationDetailResponse`:**

| Field | Type | Meaning |
| --- | --- | --- |
| `investigation_id` | `str` | The primary key, echoed back |
| `structured_report` | `object` | The whole `StructuredInvestigationReport`, verbatim |

The report is returned exactly as stored — nothing summarized, reshaped or
dropped — because a client that wants to draw the timeline needs the timeline,
not a sentence about it. It is partitioned by provenance into four sections:

| Section | Contents | Provenance |
| --- | --- | --- |
| `metadata` | `application_name`, `investigation_timestamp`, `analysis_mode`, `llm_provider`, `confidence_score`, `parser_metrics` | Run identity and ingestion health |
| `synthesis` | `root_cause`, `executive_summary`, `investigation_notes` | The prepare-output node's conclusions |
| `deterministic_outputs` | `statistics`, `timeline` | Arithmetic — reproducible from the same logs |
| `ai_insights` | `error_summary`, `pattern_summary` | The two upstream LLM nodes' conclusions |

The report is deliberately **not** re-validated field by field on the way out. It
was written by the graph and read back from JSONB; re-validating here would only
mean that a report stored by an older release fails to be served at all.

**Other statuses:** `404` (no such investigation), `422` (id over 255
characters), `503` (database unreachable).

---

### `DELETE /api/investigations/{id}`

Hard removal of an investigation record by id.

**Path parameter:** `id` — 1 to 255 characters. No request body.

**Response `200`:**

```json
{
  "status": "deleted",
  "investigation_id": "inv-graph-4b2d3f2b"
}
```

**Other statuses:** `404` (no such investigation), `503` (database unreachable).

The delete is a single statement and the decision comes from `cursor.rowcount` —
no `SELECT` to check existence first, and therefore no window in which another
request removes the row between the check and the delete.

**Deleting twice is a `404`, not a second `200`.** That is a deliberate choice
against strict idempotence: a client removing something it could not see is
looking at a stale list, and saying so is what prompts the refresh.

---

## Manual Postman Test Suite Verification

The runs below were executed manually against a live backend
(`http://127.0.0.1:8010`) and a live PostgreSQL (`localhost:5432/postgres`) with
the `investigations` table initialized by `python3 init_db.py`.

Set a Postman header of `Content-Type: application/json` on every request that
carries a body.

> The database state evolves across these five tests, in order: the table starts
> with two records, Test 3 adds a third, and Test 5 removes one. Run them in
> sequence to reproduce the responses exactly.

---

### Test 1 — Health Check

**Request**

```
GET http://127.0.0.1:8010/api/health
```

**Response `200 OK`**

```json
{
    "status": "ok",
    "message": "Backend is running"
}
```

---

### Test 2 — Paginated Metadata List

**Request**

```
POST http://127.0.0.1:8010/api/investigations
```

**Payload**

```json
{"page": 1, "limit": 10}
```

**Response `200 OK`**

```json
{
    "items": [
        {
            "investigation_id": "inv-graph-001",
            "application_name": "write_to_db investigation_id test",
            "confidence_score": 100,
            "analysis_mode": "deep",
            "llm_provider": "deepseek",
            "created_at": "2026-09-03T12:28:07.233663+03:00",
            "updated_at": "2026-09-03T12:28:07.233663+03:00"
        },
        {
            "investigation_id": "inv-graph-4b2d3f2b",
            "application_name": "write_to_db auto_id_test",
            "confidence_score": 77,
            "analysis_mode": "fast",
            "llm_provider": "gemini",
            "created_at": "2026-09-03T12:11:36.186738+03:00",
            "updated_at": "2026-09-03T12:11:36.186738+03:00"
        }
    ],
    "total": 2,
    "page": 1,
    "limit": 10,
    "total_pages": 1
}
```

Note what is **not** in the response: no `structured_report` on either item.
Both of those records hold a full multi-section report in the database, and
neither is read by this query.

---

### Test 3 — Log Investigation Execution

A complete pipeline run against a four-line Pino JSON payload, using a real
Anthropic model in `fast` mode, with an explicit `investigation_id`.

**Request**

```
POST http://127.0.0.1:8010/api/investigate
```

**Payload**

```json
{
  "application_name": "payment-service",
  "raw_logs": "{\"level\":30,\"time\":\"2026-07-29T10:59:25.610Z\",\"application\":\"booking-benchmark\",\"reqId\":\"e7888c98-5b9b-4ea0-aa98-67f0464fabea\",\"event\":\"http_request_started\",\"method\":\"POST\",\"url\":\"/scenario\",\"msg\":\"Incoming request\"}\n{\"level\":40,\"time\":\"2026-07-29T10:59:25.611Z\",\"application\":\"booking-benchmark\",\"reqId\":\"e7888c98-5b9b-4ea0-aa98-67f0464fabea\",\"event\":\"scenario_changed\",\"previousScenario\":\"normal\",\"currentScenario\":\"payment_provider_down\",\"msg\":\"Scenario changed\"}\n{\"level\":30,\"time\":\"2026-07-29T10:59:25.612Z\",\"application\":\"booking-benchmark\",\"reqId\":\"e7888c98-5b9b-4ea0-aa98-67f0464fabea\",\"event\":\"http_request_completed\",\"method\":\"POST\",\"url\":\"/scenario\",\"statusCode\":200,\"durationMs\":2,\"msg\":\"Request completed\"}\n{\"level\":30,\"time\":\"2026-07-29T10:59:26.621Z\",\"application\":\"booking-benchmark\",\"reqId\":\"2d549904-a58d-4e83-af5b-6de7a8ca0e37\",\"event\":\"http_request_started\",\"method\":\"POST\",\"url\":\"/bookings\",\"msg\":\"Incoming request\"}",
  "investigation_id": "inv-graph-003",
  "analysis_mode": "fast",
  "llm_provider": "anthropic"
}
```

**Response `200 OK`**

```json
{
    "investigation_id": "inv-graph-003",
    "db_persisted": true,
    "investigation_notes": [
        "Parser: detected log format 'json' using JSONLinesParser (confidence 1.00).",
        "Parser: parsed 4 log entries.",
        "Error analysis: fingerprinted 1 warning-level entry into 1 unique signature.",
        "Error analysis: no ERROR/CRITICAL/FATAL entries were present, so WARN/WARNING entries were analyzed instead.",
        "Timeline: bucketed 4 timestamped entries into 1 window of 10 seconds, spanning 2026-07-29T10:59:25.610000+00:00 to 2026-07-29T10:59:26.621000+00:00.",
        "Timeline: no error-level entries were found, so the error onset, peak and recovery milestones were not emitted.",
        "Successfully persisted investigation inv-graph-003 to PostgreSQL database."
    ]
}
```

Four things are worth reading off those notes, because together they are the
proof that the whole pipeline ran rather than merely returned:

- **Format detection worked.** `JSONLinesParser` at confidence `1.00` — every
  sampled line parsed as a JSON object.
- **The warning-tier fallback fired.** The payload carries no `ERROR` records,
  so the error-analysis node analyzed the single `WARN` (`level: 40`,
  `scenario_changed`) rather than reporting nothing, and said so.
- **The absent error milestones are reported, not silently missing.** An absent
  milestone means "not observed", never "assumed zero".
- **No degradation note appears.** There is no `LLM reasoning unavailable`, no
  `LLM synthesis unavailable` and no substituted-model line, so all three LLM
  nodes reached Anthropic and answered against their schemas. That absence is
  the assertion that matters — every LLM node degrades rather than fails, so a
  failed call produces a complete-looking run whose interpretation is silently
  missing, and the note is the only place it shows.

`db_persisted: true` is what unlocks Scenario A in the UI: the client refetches
the record list, which now returns three items.

---

### Test 4 — Investigation Detail Fetch

**Request**

```
POST http://127.0.0.1:8010/api/investigations/inv-graph-001
```

**Payload**

```json
{}
```

**Response `200 OK`** — the complete `structured_report` JSONB object, with all
four provenance sections. Abridged below at the marked points; the real response
for this record carries 11 timeline events and 13 investigation notes.

```json
{
    "investigation_id": "inv-graph-001",
    "structured_report": {
        "metadata": {
            "application_name": "write_to_db investigation_id test",
            "investigation_timestamp": "",
            "analysis_mode": "deep",
            "llm_provider": "deepseek",
            "confidence_score": 100,
            "parser_metrics": {
                "parser_name": "JSONLinesParser",
                "parser_confidence": 1.0,
                "detected_format": "json",
                "total_lines": 2504,
                "blank_lines": 0,
                "parsed_lines": 2504,
                "malformed_lines": 0,
                "missing_timestamp_lines": 0
            }
        },
        "synthesis": {
            "root_cause": "The incident was triggered when the booking-benchmark run activated its `payment_provider_down` scenario ...",
            "executive_summary": "...",
            "investigation_notes": ["... 13 notes ..."]
        },
        "deterministic_outputs": {
            "statistics": {
                "level_distribution": ["..."],
                "logger_distribution": ["..."],
                "severity": { "error_count": "...", "warning_count": "...", "error_ratio": "...", "warning_ratio": "..." },
                "timestamp_coverage": { "with_timestamp": "...", "without_timestamp": "...", "earliest": "...", "latest": "..." },
                "metadata_distributions": { "...": "..." }
            },
            "timeline": ["... 11 events: buckets and milestones ..."]
        },
        "ai_insights": {
            "error_summary": {
                "total_errors_analyzed": "...",
                "unique_signatures_found": "...",
                "primary_error_signature_id": "ERR_003",
                "signatures": ["..."],
                "cascading_impact_summary": "..."
            },
            "pattern_summary": {
                "anomalies": ["..."],
                "cross_logger_correlations": ["..."],
                "metadata_insights": ["..."],
                "behavioral_synthesis": "..."
            }
        }
    }
}
```

Two details in that metadata are worth explaining, because both look like bugs
and neither is.

`parser_metrics` reports a clean parse — 2,504 lines, all of them parsed, none
malformed, none missing a timestamp — which is exactly why
`confidence_score` is `100`: the deterministic scoring engine had no penalty to
apply and the error analysis nominated a primary signature.

`investigation_timestamp` is `""` on this particular record because it was
created by a direct graph run that supplied none, and no node in the graph
invents a clock reading. **Records created through `POST /api/investigate` carry
a real one**, because the API is the caller that field documents and it knows
when the run started.

Fetching a record that does not exist returns `404`:

```json
{
    "detail": "Investigation not found"
}
```

---

### Test 5 — Record Deletion

**Request**

```
DELETE http://127.0.0.1:8010/api/investigations/inv-graph-4b2d3f2b
```

**Response `200 OK`**

```json
{
    "status": "deleted",
    "investigation_id": "inv-graph-4b2d3f2b"
}
```

Repeating the same request returns `404` with
`{"detail": "Investigation not found"}` — see
[the endpoint reference](#delete-apiinvestigationsid) for why the delete is not
idempotent by design.

---

### Additional Postman checks worth keeping in the collection

These are not part of the five-test sequence above but exercise the failure
contracts a client has to handle.

| Check | Request | Expected |
| --- | --- | --- |
| Missing required field | `POST /api/investigate` with `{"application_name": "x"}` | `422`, `detail[0].loc = ["body", "raw_logs"]` |
| Unknown field | `POST /api/investigate` with `"analysis_modes": "deep"` | `422`, `type: "extra_forbidden"` |
| Invalid page | `POST /api/investigations` with `{"page": 0}` | `422`, `greater_than_equal` |
| Limit ceiling | `POST /api/investigations` with `{"limit": 101}` | `422`, `less_than_equal` |
| Empty pagination body | `POST /api/investigations` with `{}` | `200`, page 1, limit 10 |
| Generated id | `POST /api/investigate` with no `investigation_id` | `200`, id matching `inv-graph-[0-9a-f]{4}` |
| Unknown record | `POST /api/investigations/does-not-exist` | `404`, `"Investigation not found"` |
| Over-long id | `POST /api/investigations/<256 chars>` | `422` |
| CORS preflight | `OPTIONS /api/investigations` with `Origin: http://localhost:5173` | `200`, `access-control-allow-origin` echoing the origin |

A `503` on any storage endpoint means the database could not be reached. The
fastest way to confirm that from Postman is that `GET /api/health` still returns
`200` while the other three do not — health deliberately does not depend on
storage.

---

## Automated Testing Strategy

### Structure

The suite lives in `tests/test_backend_api.py`: **68 tests, all passing, all
offline.** No PostgreSQL, no LLM provider, no compiled graph.

The application under test is built through the real `create_app()` with a
substitute `ServiceFactory`. Everything below that one seam is the production
class — the real `DefaultInvestigationService`, the real
`LangGraphRunnerService`, the real routes, middleware and exception handlers.
Only the two leaves, where the database and the graph would be, are substituted.
That is the payoff of the abstractions rather than a convenience: the doubles
implement the same ABCs the production classes do, so a test that passes here is
a test against the real interfaces.

| Double | Implements | Behaviour |
| --- | --- | --- |
| `MemoryInvestigationRepository` | `InvestigationRepository` | Holds rows, honours `limit`/`offset`, records every page call |
| `UnreachableRepository` | `InvestigationRepository` | Raises `RepositoryError` from every method — the 503 path |
| `RecordingGraph` | `CompiledGraph` | Records the input state; can succeed, raise or stall |
| `StubGraphFactory` / `FailingGraphFactory` | `GraphFactory` | Hand out a stub graph, or fail to build one |
| `StubServiceFactory` | `ServiceFactory` | The single seam the application is wired through |

`MemoryInvestigationRepository` is a real implementation rather than a mock — it
honours offsets and reports `rowcount` semantics on delete — so the service's
pagination arithmetic and its not-found handling are exercised against behaviour
rather than against a recorded call.

### Coverage

| Group | What it asserts |
| --- | --- |
| Health | The exact documented body; that it survives an unreachable database |
| Investigate | Id echo and generation, defaults, the exact input-state keys sent to the graph, the injected `investigation_timestamp`, `db_persisted: false` as a 200, an empty final state, 422 on seven invalid payloads, 422 on an unknown field, 504, 500 on an unbuildable graph, 500 on a raising graph |
| List | Page contents and ordering, the absence of `structured_report`, absent and empty bodies, offset arithmetic, `total_pages` rounding, the empty table, an out-of-range page, `null` confidence preservation, six invalid payloads, 503 |
| Detail | The report returned verbatim, all four sections, an absent body, 404, an over-long id, 503 |
| Delete | Removal, its effect on the list, 404, non-idempotence, 503 |
| Error envelope | Every status uses `{"detail": ...}`; an unexpected exception is a 500 that leaks no traceback |
| CORS | Both React origins on preflight and on a real request; an unknown origin gets no allow header |
| Service layer | Both overloads dispatch on call shape; a call matching neither raises `TypeError`; keyword fields validate through the same model; a supplied id is never replaced; the generated-id format |
| Architecture | All six ABCs refuse instantiation; the OpenAPI schema documents exactly the five endpoints |

The graph-invocation tests assert on the **input state the runner built**, not
merely on the response, so a regression that stopped forwarding
`enable_web_search` or started seeding a working-region field such as
`search_context` fails immediately.

### Running the tests

```bash
pytest tests/test_backend_api.py -v
```

The backend suite needs no database and no environment, and takes well under a
second.

To run the whole repository suite — the backend plus every graph node:

```bash
python3 -m pytest -q
```

> **Point the tests at a harmless database.** The graph-level suites run the
> full pipeline end to end several times, and with a reachable PostgreSQL each
> of those runs stores a real row. That is the auto-generation behaviour working
> as designed, not a fault, but it does mean the suite writes to whatever `DB_*`
> points at:
>
> ```bash
> DB_HOST=127.0.0.1 DB_PORT=1 python3 -m pytest -q
> ```
>
> Every write then fails fast, each run records its degradation note, and no
> assertion is affected. The backend suite is unaffected either way — it never
> touches a database.

---

## Operational Notes

**The graph is compiled once per process**, on the first `POST /api/investigate`,
and cached for the lifetime of the server. The first invocation therefore pays a
one-off compilation cost on top of its own analysis; every later one does not.
Under `API_RELOAD` that cache is discarded on every file change, which is why
auto-reload is a development-only setting.

**Credentials never enter graph state or a response.** Database settings come
from the environment through `DatabaseConfig`, and LLM keys are read by
`llm_factory` from the environment. Log lines about the database are built from
a `host:port/dbname` label that has no representation for the password.

**Tracing large payloads.** If `LANGSMITH_TRACING` is on, a multi-megabyte
`raw_logs` payload will push the trace upload past LangSmith's limits and fail
on a background thread — costing observability, not results. See
[the troubleshooting section of `GRAPH_README.md`](GRAPH_README.md#troubleshooting-langsmith-ssl--timeout-errors-on-large-logs)
for the measured thresholds.

**Common startup problems.**

| Symptom | Cause | Fix |
| --- | --- | --- |
| `FAILED to bind 127.0.0.1:8010` | Port in use | Set `API_PORT` |
| `503` from all three storage endpoints, `200` from health | Database unreachable, or the table does not exist | Check `DB_*`; run `python3 init_db.py` |
| `db_persisted: false` with a note naming the database | The graph ran but could not store | Same as above — the analysis itself is unaffected |
| Notes contain `LLM reasoning unavailable` | No provider key, or an unreachable endpoint | Set the provider's key, or use `llm_provider: "local"` with the mock server |
| Browser reports a CORS error | The UI's origin is not in the allow list | Add it to `API_CORS_ORIGINS` |

# LogSherlock

LogSherlock is a log analysis platform built as a LangGraph workflow. It ingests
raw log output from a variety of production systems, turns it into a clean,
normalized, machine-readable form, and then reasons about it: what the dataset
contains, how the incident unfolded over time, which errors occurred, and which
of them actually started it.

---

## Supported Log Formats

The Parser node currently recognizes and extracts structure from the following
log ecosystems:

- **Generic text logs** — Free-form, timestamped, or level-prefixed text lines
  that do not belong to a specific ecosystem. Always parsed on a best-effort
  basis so no line is ever lost.
- **JSON Lines** — Logs where each line is a standalone JSON object. Known fields
  are mapped into the common schema and any remaining keys are preserved as
  metadata.
- **Spring Boot** — The default Spring Boot console format, including timestamp,
  level, process id, thread, logger, and message.
- **PostgreSQL** — PostgreSQL server logs, including the database-specific
  severities and per-line metadata such as process id and timezone.
- **Python logging** — The default Python `logging` output format, extracting the
  level, logger name, and message.
- **FastAPI / Uvicorn** — Uvicorn access logs as well as startup, shutdown, and
  exception output. Request lines yield structured details such as client
  address, method, path, and status code.
- **NestJS** — The default NestJS logger format, extracting timestamp, process
  id, level, component context, and the clean message.
- **Pino JSON logs** — Structured JSON logs from the Pino/Bunyan family,
  including numeric level normalization and preservation of request-scoped
  fields as metadata.
- **Microsoft SQL Server** — SQL Server ERRORLOG output, extracting the
  timestamp, message, and metadata such as session id, severity, and state.

---

## Normalized Output

Regardless of the source format, every parsed line is converted into a single
common schema. This uniform shape is what makes the rest of the system possible:
consumers work against one representation instead of many source-specific ones.

Conceptually, each normalized entry carries:

- **timestamp** — The event time, normalized into a consistent form, or empty
  when the source line does not provide one.
- **level** — The severity of the entry, when present.
- **logger** — The logger or component that emitted the entry, when present.
- **message** — The human-readable message text.
- **raw** — The original, untouched line, always preserved.
- **metadata** — Any additional structured fields the line carried that are not
  part of the common schema.

Fields that a line does not provide are simply left empty; the parser never
invents information that the source did not contain.

---


## Sample Logs & Benchmarks

The repository includes a `sample_logs/` directory containing representative log
files from the supported ecosystems as well as some intentionally mixed and
malformed inputs. These files are used for:

- parser development
- regression testing
- manual testing in LangGraph Studio
- adding support for new ecosystems
- end-to-end exercising of the statistics, timeline, error analysis, web search
  and prepare output nodes

### Small fixtures

Short, hand-written files that pin down one format or one edge case each:

- `java_spring_boot.log`
- `postgresql.log`
- `python_logs.log`
- `simple.log`
- `timestamps.log`
- `json.log`
- `fastapi.log`
- `nestjs_logger.log`
- `typescript_pino.log`
- `mssql.log`
- `mixed_formats.log`
- `malformed.log`

### Realistic benchmark datasets

Full-size, scenario-driven datasets that carry a real incident shape — a healthy
baseline, a failure, and a recovery — rather than a handful of illustrative
lines. These are the standard benchmarking inputs used across graph nodes,
because they are the only inputs large enough to exercise adaptive bucket sizing,
signature capping, traceback collation and metadata cardinality limits:

- `sample_logs/fastapi_recovery.log`
- `sample_logs/typescript_pino_recovery.log`
- `sample_logs/java_spring_boot_large.text.log`
- `sample_logs/java_spring_boot_large.json.log`

---

## logsherlock-benchmarks

The realistic datasets listed above are **generated and maintained via the
[logsherlock-benchmarks](https://github.com/AyberkYavuz/logsherlock-benchmarks)
repository**, not written by hand in this repository. That project emits
scenario-driven log output from instrumented FastAPI, Pino/TypeScript and Spring
Boot applications; the files are then committed here so every node is developed
and regression-tested against the same fixed corpus.

Two consequences are worth knowing:

- **The datasets are reproducible.** A benchmark file can be regenerated from the
  benchmarks repository rather than being a one-off capture, so a scenario can be
  extended or re-emitted when a node needs a case the corpus does not yet cover.
- **The parser tracks the generator's output shape.** The Spring Boot benchmark
  layout — `TS LEVEL [thread] logger key=value ... message`, with no pid column
  and a run of structured `key=value` fields before the human-readable text — has
  its own entry in the parser's pattern registry precisely because this is what
  the benchmarks emit.

These files also back the web-search benchmark documented in
[`docs/GRAPH_README.md`](docs/GRAPH_README.md), where
`java_spring_boot_large.json.log` is the large-dataset case.

---

## LangGraph Testing

Node correctness is validated through several complementary approaches:

- automated unit tests for each parser, normalization helper and aggregation
- dedicated suites for the statistics, timeline, pattern analysis, error
  analysis, web search and prepare output nodes, including the two-pass search
  loop and its routing, and the confidence-scoring engine penalty by penalty
- a topology suite that pins the graph's exact node and edge sets
- an architecture test that keeps shared models in the `graph_library.models` package and
  guards the dependency direction
- regression tests that guard against quiet quality drops
- a sample log corpus that exercises every supported ecosystem end to end
- a local mock LLM server covering all four response schemas, so the
  error-analysis, pattern-analysis and prepare-output paths can all be tested
  without reaching a provider
- manual verification through LangGraph Studio

Together these keep the graph stable as new nodes and ecosystems are added.

---

## LangGraph Design Principles

The graph is built around a small set of guiding principles:

- **Extensible architecture** — New log ecosystems are added by extending an
  ordered registry of patterns rather than rewriting the parser.
- **Pattern-based parsing** — Formats are described as focused, reusable patterns
  instead of one monolithic rule.
- **Common output schema** — Every format is normalized into the same shape, so
  the rest of the system depends on one representation. Every structure that
  crosses a node boundary is defined once, in the shared `graph_library.models` package.
- **Graceful degradation** — Unknown or low-quality lines still produce useful
  output; the parser never fails on unexpected input. The same holds one level
  up: a failed LLM call or an unreachable search still publishes the
  deterministic findings and records why, rather than killing the branch.
- **Ecosystem-specific extraction** — Each supported format contributes its own
  structured fields and metadata where the source provides them.
- **Determinism wherever it is available** — Everything that can be computed by
  arithmetic is, including orderings and tiebreakers; the LLM is asked only for
  what arithmetic cannot supply, and never overwrites a deterministic field.
- **Nothing is invented** — A value the source did not provide stays absent. No
  node repairs, infers or back-fills a missing timestamp, level or logger.
- **Backward compatibility** — Existing formats keep working unchanged as new
  ones are introduced.

---

## System Components Documentation

We have langgraph, backend, frontend and deployment components. 

You can find each component detail:

[`docs/GRAPH_README.md`](docs/GRAPH_README.md)

[`docs/BACKEND_README.md`](docs/BACKEND_README.md) 

[`docs/FRONTEND_README.md`](docs/FRONTEND_README.md)

[`docs/DOCKER_README.md`](docs/DOCKER_README.md) 

---

## LogSherlock Local Setup

### Prerequisites

You need to have the followings on your local machine:

* Node 22+
* Python 3.12+
* uv package manager
* Postgres

### Local  Setup

After cloning the repository, please run the following command:

```bash
cp .env.example .env
```

You need to fill your API keys in .env file.

When you have filled .env file, please run the following command in order to create investigations table on your local Postgres:

Terminal 1:
```bash
uv run init_db.py
```

After running init_db.py, you will have investigations table. LogSherlock cannot be run without that table.

Run the following command to up mock local llm service. This service simulates OpenAI compatible local llm service.

Terminal 2:
```bash
uv run tests/mock_local_llm.py
```

Now we are ready to run backend application of LogSherlock.

Please run the following command to start backend application:

Terminal 3:
```bash
uv run backend.py
```

Please run the following command to start frontend application:

Terminal 4:
```bash
cd frontend && npm run dev
```

Please copy the following files from sample_logs/ folder and paste them to your Desktop:

* fastapi_recovery.log
* typescript_pino_recovery.log
* java_spring_boot_large.text.log
* java_spring_boot_large.json.log

You can use these files to test LogSherlock on your local machine.

### LogSherlock Local Deployment

### Prerequisite

You need to have the following on your local machine:

* Docker Desktop

### Local Deployment

After cloning the repository, please run the following command in log-sherlock (root) directory:

```bash
cp .env.example .env.docker
```

You need to fill your API keys in .env.docker file.

When you have filled .env.docker file, please run the following command in order to have required images for deployment.

Terminal 1:

```bash
docker compose --env-file .env.docker build
```

When you have the following images, you are ready to run the containers.

* logsherlock-mock-llm
* logsherlock-backend
* logsherlock-frontend

To run LogSherlock, please run the following command:

```bash
docker compose --env-file .env.docker up -d  
```

When your containers are up, hit the given frontend url (described in .env.docker ex: API_CORS_ORIGINS=http://localhost:3000)

Try to insert an investigation record to postgres via UI.

Check the record on postgres container via clicking postgres container after that click exec to run the following commands:

```bash
psql -U postgres # first command that enables running sql commands on postgres container

select investigation_id, application_name, analysis_mode, llm_provider from investigations; # second command to see the records

Type \q and press Enter # This is the standard PostgreSQL quit command
```

Run the following commands to stop containers and delete everything:

```bash
docker system prune -a -f # It deletes almost everything cached by Docker across your entire system to free up disk space.

docker compose down -v --rmi all # It tears down the containers defined in your current docker-compose.yml file and wipes their associated data.
```

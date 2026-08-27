# LogSherlock

LogSherlock is a log analysis platform built as a LangGraph workflow. It ingests
raw log output from a variety of production systems, turns it into a clean,
normalized, machine-readable form, and then reasons about it: what the dataset
contains, how the incident unfolded over time, which errors occurred, and which
of them actually started it.

The project is under active development. Its architecture is designed to grow
one analysis stage at a time, and this document describes only the parts that
exist in the repository today.

---

## Current Status

The graph pipeline has expanded well beyond parsing. Six nodes are now active
and fully operational:

- **`parser`** — deterministic ingestion. Detects the log format, parses every
  line and normalizes it into a common schema, and reports structured parser
  health metrics.
- **`statistics`** — deterministic dataset composition. Level and logger
  distributions, severity counts and ratios, timestamp coverage, and
  distributions over dynamically discovered metadata keys.
- **`timeline`** — deterministic temporal analysis. Adaptively sized time
  buckets plus the milestones that make the shape readable: log coverage
  boundaries, first and last error, and the error onset → peak → recovery
  narrative.
- **`pattern_analysis`** — behavioral reasoning over the two deterministic
  reports above, which is why it runs downstream of them rather than beside
  them. Reports volume spikes, cross-logger cascades, metadata concentrations
  and baseline shifts. When no model is reachable it derives the same summary
  arithmetically rather than returning nothing.
- **`error_analysis`** — deterministic error fingerprinting followed by a single
  batched LLM pass. Collates multi-line tracebacks, masks variable tokens,
  collapses identical failures into counted signatures, and asks a model which
  signature is the root cause and how it cascaded. Five providers are supported
  across three reasoning tiers.
- **`web_search`** — the optional, opt-in detour between the two error-analysis
  passes. Retrieves external documentation from Tavily for error signatures a
  model cannot be expected to recognise, behind a relevance floor.

Full per-node documentation — state contracts, algorithms, guarantees, provider
handling and the web-search benchmark — lives in
[`docs/GRAPH_README.md`](docs/GRAPH_README.md).

The remaining nodes in the topology are still deterministic stubs, and this
documentation will grow together with the implementation.

---

## Parser Node

The Parser node is the entry point for all log analysis. It takes raw log text
and produces a consistent, structured representation that the rest of the system
can rely on. Its responsibilities are:

- **Detect the log format** of the incoming data automatically.
- **Parse supported log formats** from many different ecosystems.
- **Normalize every line into a common schema**, so downstream consumers never
  need to know which system produced the logs.
- **Extract the timestamp** and normalize it into a consistent representation.
- **Extract the log level** (severity) where present.
- **Extract the logger or component name** where present.
- **Extract structured metadata** that a line carries beyond the common fields.
- **Produce parser metrics** describing the health of each parsing run.
- **Never fail because of unknown or unexpected lines.** A line that cannot be
  fully understood is still preserved rather than dropped or raised as an error.
- **Gracefully handle mixed-quality logs**, extracting as much structure as a
  line offers and falling back cleanly when a line is unstructured.

The parser is deterministic: the same input always produces the same output.

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

## Parser Metrics

Every parsing run produces a set of metrics that summarize how the run went.
These metrics make the health of the ingestion stage observable and let later
stages reason about data quality. They include:

- **detected format** — The log format the parser identified for the input.
- **parser used** — Which parser handled the input.
- **confidence** — How strongly the input matched the selected format.
- **total lines** — The total number of lines in the input.
- **parsed lines** — The number of lines successfully turned into entries.
- **malformed lines** — Non-empty lines the parser could not parse.
- **blank lines** — Empty or whitespace-only lines that were skipped.
- **missing timestamps** — Parsed entries that did not carry a timestamp.

---

## Sample Logs & Benchmarks

The repository includes a `sample_logs/` directory containing representative log
files from the supported ecosystems as well as some intentionally mixed and
malformed inputs. These files are used for:

- parser development
- regression testing
- manual testing in LangGraph Studio
- adding support for new ecosystems
- end-to-end exercising of the statistics, timeline, error analysis and web
  search nodes

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

## Testing

Node correctness is validated through several complementary approaches:

- automated unit tests for each parser, normalization helper and aggregation
- dedicated suites for the statistics, timeline, pattern analysis, error
  analysis and web search nodes, including the two-pass search loop and its
  routing
- a topology suite that pins the graph's exact node and edge sets
- an architecture test that keeps shared models in the `graph_library.models` package and
  guards the dependency direction
- regression tests that guard against quiet quality drops
- a sample log corpus that exercises every supported ecosystem end to end
- a local mock LLM server so the error-analysis paths can be tested without
  reaching a provider
- manual verification through LangGraph Studio

Together these keep the graph stable as new nodes and ecosystems are added.

---

## Design Principles

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

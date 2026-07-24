# LogSherlock

LogSherlock is a log analysis platform built as a LangGraph workflow. It ingests
raw log output from a variety of production systems and turns it into a clean,
normalized, machine-readable form that later analysis stages can reason about.

The project is under active development. Its architecture is designed to grow
one analysis stage at a time, and this document describes only the parts that
exist in the repository today.

---

## Current Status

At present, the ingestion and parsing stage is the only fully implemented
analysis component. The workflow wiring and the coordination layer that route
work through the graph exist, but the Parser node is the single component that
performs real log analysis.

This documentation will grow together with the implementation. As new stages are
built, they will be documented here alongside the Parser node.

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

## Sample Logs

The repository includes a `sample_logs/` directory containing representative log
files from the supported ecosystems as well as some intentionally mixed and
malformed inputs. These files are used for:

- parser development
- regression testing
- manual testing in LangGraph Studio
- adding support for new ecosystems

The sample logs currently available are:

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

---

## Testing

Parser correctness is validated through several complementary approaches:

- automated unit tests for each parser and normalization helper
- regression tests that guard against quiet quality drops
- a sample log corpus that exercises every supported ecosystem end to end
- manual verification through LangGraph Studio

Together these keep the parser stable as new ecosystems are added.

---

## Design Principles

The parser is built around a small set of guiding principles:

- **Extensible architecture** — New log ecosystems are added by extending an
  ordered registry of patterns rather than rewriting the parser.
- **Pattern-based parsing** — Formats are described as focused, reusable patterns
  instead of one monolithic rule.
- **Common output schema** — Every format is normalized into the same shape, so
  the rest of the system depends on one representation.
- **Graceful degradation** — Unknown or low-quality lines still produce useful
  output; the parser never fails on unexpected input.
- **Ecosystem-specific extraction** — Each supported format contributes its own
  structured fields and metadata where the source provides them.
- **Backward compatibility** — Existing formats keep working unchanged as new
  ones are introduced.

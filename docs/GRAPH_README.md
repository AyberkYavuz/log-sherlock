# LogSherlock Graph Nodes

LogSherlock is a log analysis platform built as a LangGraph workflow. This
document describes the nodes of that graph that are **fully implemented today**:

- **Parser** — turns raw log text into normalized entries
- **Statistics** — reports what the parsed dataset contains
- **Timeline** — reports how the incident unfolded over time
- **Pattern Analysis** — reads those two reports and says what about them is abnormal
- **Error Analysis** — groups the errors and explains which one started it
- **Web Search** — optionally fetches external documentation for obscure errors

Each node is described from its Python source. Nodes that are still stubs in
`graph.py` are deliberately not documented here.

---

## How the Nodes Fit Together

Every node has the same signature: it accepts the full graph state and returns a
*partial* state delta containing only the keys it owns. No node mutates state in
place.

The topology is fixed. `parser` fans out into three parallel branches, and every
analysis stage fans back in to `recommendation`:

```
START
  -> parser -----------------------------------------------------------------------+
  -> [ error_analysis (LLM) <-> web_search (network) ] ---------------------------+|
  -> [ statistics (deterministic), timeline (deterministic) ]                     ||
         |                                       |                                ||
         +---------------------------------------+-> pattern_analysis (LLM) ------++-> recommendation -> report_generator -> END
         |                                                                        |
         +------------------------------------------------------------------------+
```

Three things about that shape are worth stating plainly:

- **`pattern_analysis` is downstream of the deterministic pair, not parallel to
  it.** The patterns it looks for are properties of `statistics` and `timeline`
  output — the distributions one produces, the buckets and milestones the other
  does — so it consumes both rather than re-reading `parsed_logs`. Two plain
  edges into one node is a join: it runs once, after *both* have landed.
- **`recommendation` takes a direct edge from all four analysis stages.** It
  needs `statistics` and `timeline` in raw form as well as the patterns derived
  from them, so those two feed it directly in addition to feeding
  `pattern_analysis`. `error_analysis` is the exception only in mechanism: it
  arrives via `route_after_error_analysis` rather than a plain edge, because the
  same branch point also owns the web-search detour.
- **`recommendation` also takes a direct edge from `parser`.** That fourth edge
  out of `parser` carries `parser_metrics`, which reaches the synthesis no other
  way: every analysis stage publishes its own artifact rather than forwarding
  its inputs, so `statistics` deliberately omits parser health and `timeline`
  reads the metrics without republishing them. The edge is what lets a
  conclusion be *qualified* rather than merely stated — a root cause inferred
  from a payload where a third of the lines were malformed, or where most
  entries carried no timestamp, warrants a lower confidence score and an
  explicit data-quality caveat in the report.

The one deviation from "fixed" is the `error_analysis ↔ web_search` loop, and it
is bounded to a single lap. `web_search` always writes `search_context` — a list
on every path, including every failure path — and `route_after_error_analysis`
only routes toward search while that field is `None`. The loop is off by default;
see [Web Search Node](#web-search-node).

Two state channels use additive reducers because several nodes write to them in
the same superstep:

- **`investigation_notes`** — human-readable observations from any node
- **`completed_stages`** — the name of each node that finished

Every other field documented below has exactly one writer and therefore needs no
reducer.

---

## Parser Node

**Module:** `graph_library/parser/parser_node.py` · **Entry point:** `parser_node(state)`

The Parser node is the entry point for all log analysis. It converts `raw_logs`
into normalized `parsed_logs` — the single source of truth every downstream node
consumes. It is fully deterministic: no LLM, no prompts, no network. The same
`raw_logs` always produces the same result.

**Reads:** `raw_logs`
**Writes:** `parsed_logs`, `parser_metrics`, `investigation_notes`,
`completed_stages`

### What it does

- **Detects the log format** by sampling the first 50 non-blank lines and scoring
  each registered parser against them (`graph_library/parser/parser_factory.py`).
- **Parses every line** with the winning parser, tolerating malformed input.
- **Normalizes each line into a common schema**, so downstream consumers never
  need to know which system produced the logs.
- **Extracts the timestamp** and normalizes it into a `datetime` — once, here.
  Downstream nodes never re-parse timestamps.
- **Extracts the log level, logger name and message** where present.
- **Extracts structured metadata** that a line carries beyond the common fields.
- **Produces parser metrics** describing the health of each run.
- **Never fails on unknown input.** Blank lines are skipped and counted;
  unparseable lines are skipped and counted; nothing raises.

### Format detection

Detection is deterministic. Each structured parser scores the sample in `[0.0,
1.0]` and only replaces the plain-text fallback when it scores **strictly
higher** — so the fallback wins every tie, including empty input, and detection
always returns a parser rather than `None`.

| Parser | `LogFormat` | Confidence rule |
| --- | --- | --- |
| `JSONLinesParser` | `json` | Fraction of sampled lines that parse as JSON *objects* (bare arrays and scalars do not count) |
| `PlainTextParser` | `text` | A flat `0.1` baseline for any non-empty sample — the universal fallback |

Adding a format means adding a parser to `PARSER_REGISTRY`; detection and node
wiring pick it up automatically.

### Supported log shapes

`JSONLinesParser` handles any line that is a standalone JSON object, mapping
known fields via the alias tables in `graph_library/parser/normalization.py` (including numeric
Pino/Bunyan levels, `30` → `INFO`) and preserving every remaining key as
metadata.

`PlainTextParser` is an engine over an ordered registry of focused patterns
(`graph_library/parser/patterns.py`), tried most-specific first:

- **Spring Boot** — `TS LEVEL pid --- [thread] logger : message`, with `pid` and
  `thread` lifted into metadata.
- **Spring Boot (benchmark shape)** — `TS LEVEL [thread] logger key=value ... message`,
  the layout emitted by the `logsherlock-benchmarks` generator. The `key=value`
  run (application, scenario, reqId, service, …) is lifted into metadata
  dynamically.
- **PostgreSQL** — `TS TZ [pid] SEVERITY: message`, with `pid` and `tz` metadata.
- **Python logging** — the default `LEVEL:logger:message` output.
- **FastAPI / Uvicorn access** — request lines yield `client_ip`, `client_port`,
  `method`, `path` and `status_code` metadata under the `uvicorn.access` logger.
- **NestJS** — `[Nest] pid - TS LEVEL [context] message`.
- **Microsoft SQL Server** — ERRORLOG output, with `spid`, `error_number`,
  `severity` and `state` metadata.
- **Level-prefixed lines** — `LEVEL: message`, optionally timestamp-fronted.
  FastAPI/Uvicorn startup, shutdown and exception lines land here.
- **Generic fallbacks** — progressively less specific shapes, down to
  `LEVEL message` and `TS message`.

If no pattern matches, the line still produces an entry: a leading timestamp and
level are salvaged opportunistically and the remainder becomes the message. A
plain-text line is never "malformed", it just carries less structure.

### Normalized output — `ParsedLogEntry`

| Field | Meaning |
| --- | --- |
| `line_number` | 1-based position in the raw text, preserved across skipped lines |
| `raw` | The original, untouched line |
| `timestamp` | Normalized `datetime`, or `None` when absent, unparseable or *incomplete* |
| `level` | Upper-cased severity, or `None` |
| `logger` | Logger / component name, or `None` |
| `message` | Human-readable message, falling back to `raw` |
| `metadata` | Additional structured fields; always a dict, possibly empty |

The parser never invents information. A timestamp that omits part of the date —
yearless syslog's `Jan 10 14:52:31`, for instance — yields `None` rather than a
value completed from context, because `strptime` would otherwise silently supply
the year 1900 and that reads downstream as a real event time.

Recognized timestamp formats are ISO 8601 (via `datetime.fromisoformat`,
including `Z` suffixes, explicit offsets, space separators and fractional
seconds) and the US 12-hour NestJS shape `07/22/2026, 10:15:30 AM`. Numeric
epochs and anything else unrecognized yield `None`.

### Parser metrics

`ParserMetrics` is the machine-readable half of the parser's report — the
Timeline node reads `missing_timestamp_lines` from it rather than recounting.

| Field | Meaning |
| --- | --- |
| `parser_name` | Class name of the selected parser |
| `parser_confidence` | Detection confidence on the sampled input |
| `detected_format` | The chosen `LogFormat` value |
| `total_lines` | Total lines in `raw_logs`, blanks included |
| `blank_lines` | Empty / whitespace-only lines, skipped |
| `parsed_lines` | Lines successfully turned into entries |
| `malformed_lines` | Non-blank lines that could not be parsed |
| `missing_timestamp_lines` | Parsed entries whose `timestamp` is `None` |

The invariant `total_lines == blank_lines + parsed_lines + malformed_lines`
always holds. The same facts are also phrased for humans in
`investigation_notes`.

---

## Statistics Node

**Module:** `graph_library/stats/statistics_node.py` · **Entry point:** `statistics_node(state)`

The Statistics node answers exactly one question about the parser's output —
*"what does the parsed dataset contain?"* — and answers it with facts only: no
LLM, no prompts, no network, no interpretation.

**Reads:** `parsed_logs`
**Writes:** `statistics`, `completed_stages`

Internally the aggregation runs on a pandas `DataFrame` (`graph_library/stats/aggregations.py`),
but the payload that leaves the module is plain, JSON-serializable Python — no
DataFrame ever enters graph state.

### Scope boundaries

- **Parser health** (total / parsed / malformed counts) belongs to
  `parser_metrics` and is never mirrored here. `parser_metrics` is deliberately
  *not* read by this node.
- **Temporal behaviour** (buckets, spikes, onset, recovery) belongs to the
  Timeline node. Statistics reports only dataset-level timestamp coverage.

### Output — `Statistics`

| Field | Meaning |
| --- | --- |
| `level_distribution` | Counts per observed `level`, most frequent first, capped at the top 20. Records without a level appear as a `None` value |
| `logger_distribution` | The same, for `logger` |
| `severity` | `error_count`, `warning_count` and their ratios |
| `timestamp_coverage` | `with_timestamp`, `without_timestamp`, `earliest`, `latest` |
| `metadata_distributions` | A distribution per *dynamically discovered* metadata key, in alphabetical order |

Distributions are lists of `{value, count}` rows rather than `{value: count}`
mappings, which makes the ordering an explicit part of the payload and keeps
non-string values (ints, floats, booleans, `None`) representable without
stringifying them.

### Rules the aggregations hold

- **Determinism.** Identical `parsed_logs` always yield an identical result,
  ordering included. Distributions sort by descending count with an explicit
  `(str(value), type name)` tiebreaker; metadata keys are emitted alphabetically.
  Nothing depends on dict or set iteration order.
- **Ecosystem independence.** No metadata field name is hard-coded — keys are
  discovered from the records themselves.
- **Spelling normalization only.** `WARN` and `WARNING` fold together because
  they are the same level written differently. `FATAL` and `CRITICAL` are
  deliberately *not* folded into `error_count`; they stay visible as distinct
  levels in `level_distribution` rather than being silently promoted.
- **Ratios are shares of the whole dataset.** Records without a level are
  included in the denominator, so the numbers read as "share of all records".
- **Metadata keys are all-or-nothing.** A key appears only when at least one
  record carries a meaningful value, *every* meaningful value is a scalar
  (`str`/`bool`/`int`/`float`), and the number of distinct values is at most 21.
  Otherwise the key is omitted entirely — no partial distribution, no
  "high cardinality" marker.
- **Timestamps are used exactly as the parser normalized them.** A UTC-normalized
  copy is used purely as a sort key so naive and aware datetimes can be compared
  without raising; the reported `earliest` / `latest` are the original values.

An empty `parsed_logs` list is valid and yields an empty-but-well-formed payload
(zero counts, empty distributions, `None` timestamps) rather than an error.

---

## Timeline Node

**Module:** `graph_library/timeline/node.py` · **Entry point:** `timeline_node(state)`

The Timeline node answers *"how did this incident unfold over time?"* — with
arithmetic only. Given the same `parsed_logs` it always returns the same
timeline, in the same order, with the same wording.

**Reads:** `parsed_logs`, `parser_metrics`
**Writes:** `timeline`, `investigation_notes`, `completed_stages`

### Adaptive granularity

The node never asks the caller how finely to slice time; it derives the bucket
width from the span of the logs themselves, so a 30-second burst and a week-long
incident both produce a readable number of buckets. Boundaries are asymmetric on
purpose so each span belongs to exactly one band:

| Log span (ΔT) | Bucket width |
| --- | --- |
| ΔT &lt; 5 minutes | 10 seconds |
| 5 minutes ≤ ΔT ≤ 1 hour | 1 minute |
| 1 hour &lt; ΔT ≤ 24 hours | 15 minutes |
| ΔT &gt; 24 hours | 1 hour |

Buckets are aligned to midnight of the value's own day, so they land on the clock
ticks a reader expects (`12:00:00`, `12:15:00`, …) rather than at an arbitrary
offset inherited from the first log line. Every width above divides a day evenly,
so the alignment is exact.

The series is **contiguous** — empty buckets are built and then dropped from the
event list, because an empty window is meaningful evidence and is what makes
recovery detection possible. The count of dropped windows is reported in the
notes.

### Bucket events

Each populated bucket becomes one `TimelineEvent` with `event_type: "bucket"`
carrying the window's start and exclusive end, `total_logs`, `error_count`,
`warning_count`, up to 3 `top_loggers`, up to 2 `sample_messages` and a
deterministic one-line `summary`.

- **Loggers are ranked by error volume first, then total volume, then name.**
  During an incident the component producing the failures matters more than the
  chattiest one.
- **Sample messages put errors first**, and within each group the earliest win —
  the first occurrence of a failure is more informative than a later repeat.
  Previews are collapsed to one line and truncated at 200 characters.

Note that the timeline's error vocabulary (`ERROR`, `CRITICAL`, `FATAL`) is
deliberately *wider* than the Statistics node's: onset, peak and recovery are
questions about failure volume, not about which word an ecosystem uses.

### Milestones

Seven kinds of `event_type: "milestone"` events mark the moments a human should
look at first:

| `milestone_kind` | Meaning |
| --- | --- |
| `logs_start` / `logs_end` | First / last usable timestamp in the payload |
| `first_error` / `last_error` | First / last entry at an error severity |
| `error_onset` | The bucket where error volume breaks out of its prior baseline |
| `peak_error_volume` | The bucket with the highest error count; ties go to the earliest |
| `recovery_onset` | The first post-peak bucket back at that baseline |

`logs_start` and `logs_end` are always present. The three error-shaped milestones
form one narrative and are emitted only when the payload contains error-level
entries at all — an absent milestone means "not observed", never "assumed zero".

Onset detection scans forward keeping the running mean of the error counts seen
*so far*: a bucket is the onset when it contains at least one error and that
count exceeds twice the running mean. Because the mean over an error-free prefix
is zero, this reduces to "the first bucket containing an error" for the common
case where a payload begins quietly, and to a genuine spike test once errors are
already part of the baseline. Recovery reuses that same baseline as its "back to
normal" threshold, so the pair reads symmetrically.

### Ordering and scope

Events are ordered strictly by time with a fully specified tiebreaker: events
sharing an instant are ordered milestones-first, then by narrative order
(`logs_start` → `first_error` → `error_onset` → `peak_error_volume` →
`recovery_onset` → `last_error` → `logs_end`). The output is byte-identical
across runs and platforms.

Scope boundaries the node respects:

- **Timestamps are never repaired.** Entries the parser could not stamp are
  unbucketable; they are excluded and *reported* via a `Data Quality Warning`
  note, never guessed into place. The count comes from `parser_metrics`, with a
  recount from `parsed_logs` as the fallback for isolated unit tests.
- **Dataset composition** belongs to the Statistics node and is not mirrored.
- **Interpretation** belongs to the LLM nodes. Every string this node emits is a
  mechanical restatement of a count it computed.

When nothing in the payload can be placed on a time axis, `timeline` is `[]` and
a single note says so verbatim.

---

## Pattern Analysis Node

**Module:** `graph_library/pattern_analysis/node.py` · **Entry point:** `pattern_analysis_node(state)`

The Pattern Analysis node answers *"how did this system behave, and what about
that behaviour is abnormal?"* — a question neither of its inputs can answer
alone. Statistics reports what the dataset contains and Timeline reports how it
unfolded; neither is allowed to interpret its own output, and no node before
this one reads both.

**Reads:** `statistics`, `timeline`, `investigation_notes`, `llm_provider`,
`analysis_mode`, `application_name`
**Writes:** `pattern_summary`, `completed_stages`, and `investigation_notes`
when there is something to record

This is why the node sits *downstream* of the deterministic pair rather than
beside it. It never reads `parsed_logs`: everything it reasons about is already
aggregated, which is also what keeps a 700k-line payload inside one prompt.

### What reaches the model

`graph_library/pattern_analysis/prompts.py` is the adapter between two payloads
built for different readers. It serializes both as JSON — the same choice the
Error Analysis node makes, for the same reason: the model has to echo logger
names and timestamps back exactly, and JSON keeps the mapping between a value
and the thing it describes unambiguous.

Three decisions shape what gets sent:

- **Milestones are never dropped, buckets are.** A long incident produces
  hundreds of buckets and at most seven milestones, and the milestones carry the
  narrative. When the series must be trimmed to `MAX_TIMELINE_BUCKETS` (60) the
  busiest are kept — a quiet window is the least informative thing in the
  series — chronological order is then restored, and the omission is stated in
  the prompt rather than hidden. A model told it has the whole series reads a
  gap as a quiet period.
- **Investigation notes are included.** They are where the parser and the
  timeline record what they could *not* do. A model reading distributions with
  no idea that a third of the payload never reached them will overstate what
  they mean.
- **Absent inputs are stated, not rendered empty.** `{}` reads as "the dataset
  was empty"; the section says the report itself is missing.

The system prompt is written against the two failure modes a model shows on
aggregate log data: narrating the input back ("there were 412 errors, mostly
from the order service") instead of identifying what is *abnormal* about it, and
inventing a cascade between components that merely appear in the same list.
Sequence is treated as evidence for propagation; co-occurrence is not.

### Output — `PatternSummary`

| Field | Meaning |
| --- | --- |
| `anomalies` | Individually reportable behaviours, each with a `category`, `severity`, `description`, `affected_loggers` and `time_window` |
| `cross_logger_correlations` | Connections between failures in different components, one sentence each |
| `metadata_insights` | The dimensions along which activity or failures concentrate |
| `behavioral_synthesis` | A narrative of how the system's behaviour evolved |

`category` is a closed vocabulary — `volume_spike`, `logger_cascade`,
`metadata_clustering`, `baseline_shift` — because a downstream consumer has to
filter and count these, which is impossible when the model invents a new
category per run. `severity` is three tiers rather than a numeric score: a model
cannot calibrate 0–100 consistently across runs, but it can tell "worth knowing"
from "worth paging someone". An empty `anomalies` list is a valid and expected
answer for a payload that behaved normally.

### Degradation

The node degrades rather than fails, and it degrades further than the other LLM
nodes do: `graph_library/pattern_analysis/fallback.py` *derives* a pattern
summary from the same two inputs using arithmetic only. Three of the four
categories are decidable without judgement — the Timeline node already located
the onset and the recovery, the peak against the series mean is a ratio, and one
value holding most of a distribution is a share.

`logger_cascade` is the exception and is reported conservatively: the fallback
can see that component A's errors precede component B's, which is *consistent*
with propagation, and says exactly that rather than claiming causation. It is
held to the same line the system prompt holds the model to.

Because both paths return the same `PatternAnalysisResult`, a degraded run and a
healthy one publish an identical shape — the difference shows up in
`investigation_notes`, never in the schema, so no downstream consumer has to ask
which one it got. The thresholds are named constants (`SPIKE_RATIO = 2.0`, the
same multiple the Timeline node uses for onset detection, so the two passes agree
on what "breaking out of the baseline" means; `DOMINANCE_RATIO = 0.7`, high on
purpose because a metadata key reaches the distribution only when it has at most
21 distinct values, so a merely-uneven split is the normal case).

When both inputs are absent the node skips the call entirely rather than
spending a request to be told the input was empty.

### Model selection

The node reuses `graph_library/error_analysis/llm_factory.py` wholesale — the
same five providers, the same three tiers, the same `(provider, mode)` routing,
the same `MODEL_FALLBACKS` chain and the same `is_model_unavailable`
classification. Only a model-identity failure is retried; an expired key, a rate
limit or a timeout is raised straight through and degrades. A substitution is
always recorded in `investigation_notes`, since a silent one would make the
report unreproducible.

---

## Error Analysis Node

**Module:** `graph_library/error_analysis/node.py` · **Entry point:** `error_analysis_node(state)`

The Error Analysis node answers *"which errors happened, how often, and which of
them actually started the incident?"* in two clearly separated passes: a
deterministic fingerprinting pass, then an LLM reasoning pass.

**Reads:** `parsed_logs`, `llm_provider`, `analysis_mode`, `application_name`,
`enable_web_search`, `search_context`
**Writes:** `error_summary`, `investigation_notes`, `completed_stages`, and
either `search_queries` (decision pass) or `search_context` (when it decided no
search was needed)

### The deterministic pass — fingerprinting

`graph_library/error_analysis/fingerprint.py` is pure arithmetic and regex. It exists to make
the LLM pass *possible*: a 700k-line log can hold tens of thousands of error
records that are really a handful of distinct failures repeated, and sending them
raw would blow any context window and bury the signal.

1. **Filter.** Keep only records at an error severity — `ERROR`, `CRITICAL`,
   `FATAL`, `SEVERE`, `EMERGENCY`, `EXCEPTION`. Only when there are *none* does
   the warning tier (`WARN`, `WARNING`) apply, and the two are never mixed: a
   payload containing three real `ERROR`s should not have its analysis diluted by
   nine hundred warnings. The fallback is reported in the notes.
2. **Collate.** A parser sees one line at a time, so a Python traceback or a Java
   stack trace arrives as N separate unlevelled entries — with the exception type
   and message, the most diagnostic part of the record, detached from the `ERROR`
   line that introduced it. Adjacent continuation lines are reattached. Once a
   `Traceback (most recent call last):` line opens a Python traceback the walk
   switches to absorbing *every* adjacent unlevelled line until the exception
   line closes the record, because a traceback body contains arbitrary source
   code no pattern could recognize. A levelled line always ends the record; gaps
   of more than 2 source lines and runs of more than 60 continuation lines are
   hard stops.
3. **Mask.** Every variable token is replaced with a stable placeholder so two
   occurrences of the same failure produce byte-identical text: `<UUID>`,
   `<IP>`, `<PORT>`, `<ADDR>`, `<HEX>`, `<NUM>`. Rule order is load-bearing and
   runs most-specific first — a UUID must be consumed before the numeric rule can
   eat its digits, a pointer-width `0x7f…` address before the generic hex rule,
   a full timestamp before its components. The IPv6 rule carries lookarounds
   specifically so a `::` scope operator (`std::vector`, `Service::run`) is not
   mistaken for an address and every C++, Rust, PHP and Ruby symbol in a stack
   trace survives intact.
4. **Group and cap.** Identical `(template, severity)` pairs collapse into a
   counted signature — severity is part of the key so a `CRITICAL` and an `ERROR`
   that mask to the same text stay distinguishable. Signatures are ranked by
   descending count (first-appearance order breaks ties) and at most **25** are
   sent to the LLM. Anything dropped is stated in the notes, never left silent.

Each `ErrorSignature` carries `signature_id` (`ERR_001` is always the loudest),
`template`, `severity`, `count`, `first_seen` / `last_seen` (ISO-8601, falling
back to `"line 81"` when the group carries no timestamp), `loggers`, and up to 2
*unmasked* `sample_messages` so the model sees real values alongside the
template. `is_root_cause_candidate` and `explanation` are left at their empty
defaults — this pass has no basis for either, and inventing one would be exactly
the interpretation it is meant to avoid.

### The LLM pass — reasoning

The reasoning is a **single batched call**: every signature goes into one prompt
and comes back in one structured response. One call per signature was rejected
for three reasons — root cause is a *comparative* judgement (deciding a
payment-provider outage caused the booking failures is impossible while looking
at either alone); per-signature calls would multiply latency and cost by the
signature count in a node that already runs in parallel with two others; and
the response schema naturally carries summary-level fields that only exist for
the batch as a whole.

The system prompt is written to fight the two failure modes a model reliably
shows on log data:

- **Blast radius is not causation.** A high `count` indicates how many requests
  were affected, not what broke. A single infrastructure error occurring 3 times
  routinely causes 900 downstream request failures; the root cause is usually the
  low-count error that appears *first*.
- **Nothing may be invented.** Every claim must be grounded in the signatures
  provided, `first_seen` ordering is treated as causal evidence, and every
  signature id must be echoed back exactly as written.

The model returns an `LLMErrorAnalysisResult`: one
`primary_error_signature_id` (or `null`), a `cascading_impact_summary`, and one
evaluation per signature. Merging is defensive — the deterministic fields are
authoritative and never overwritten, the model contributes exactly two fields per
signature, a verdict naming an unknown id is discarded and reported rather than
force-fitted, and signatures the model skipped keep their deterministic defaults.

### Provider and model selection

`graph_library/error_analysis/llm_factory.py` maps a `(provider, mode)` pair to a configured
LangChain chat model. Five providers are supported — `openai`, `anthropic`,
`gemini`, `deepseek` and `local` (any OpenAI-compatible server: vLLM, Ollama,
LM Studio, or `tests/mock_local_llm.py`) — across three tiers: `fast`,
`standard` and `deep`. The defaults are `openai` / `standard`.

Three rules hold for every provider:

- **`temperature=0.0` wherever the model accepts it.** This node classifies and
  attributes errors; the same log should yield the same verdict twice in a row.
  Two families are documented exceptions because they *reject* the parameter
  rather than ignore it: OpenAI's reasoning models (the `o` series and GPT-5) and
  Anthropic's current generation.
- **Credentials come from the environment**, never from graph state, so an API
  key cannot leak into a report or a checkpoint.
- **Provider SDKs are imported lazily.** Only the provider actually in use has to
  be installed.

Free-text `llm_provider` values are normalized and de-aliased (`google`,
`claude`, `gpt`, and observed misspellings all resolve), since the value arrives
from a form field with nothing between it and this module.

Model ids rot, and a retired id fails at the provider with a bare `404` that says
nothing about which tier asked for it. Three layers guard against that, in
increasing order of desperation: the tier's pinned model, a table of
hand-verified `MODEL_FALLBACKS`, and a cached best-effort `discover_models()`
listing of what the configured key can reach. Two classes of failure are retried
against the next candidate, because both are properties of the *model* rather
than of the account calling it: a model-identity failure, and an unusable
answer — no tool call at all, or tool arguments that do not validate against the
schema. An expired key, a rate limit or a timeout is raised straight through,
because swapping the model would burn another request and fail the same way. A
substitution is always recorded in `investigation_notes`, since a silent one
would make the report unreproducible.

Structured output is a tool call underneath, and its arguments are handed to the
schema verbatim, so a model that nests them one level deeper — `claude-opus-5`
was observed returning `{"content": {...}}` — reads as a schema with every
required field missing. `LLMErrorAnalysisResult` and `LLMSearchDecision` unwrap
that envelope themselves, which repairs it inside the provider's own parser
where the node never sees the payload; anything left unrepairable is caught in
the fallback loop and degrades into a note rather than an exception. The
validation deliberately happens *inside* that loop: at the call site it would
sit outside the node's `try` and crash the graph thread.

Provider quirks are pinned as data rather than discovered per run. DeepSeek needs
both `method="function_calling"` (it rejects `response_format:
{"type": "json_schema"}`) and `tool_choice="auto"` (its thinking-mode models
reject a forced tool choice); either failure is total, costing the entire
root-cause pass.

### Degradation

The node degrades rather than fails. An empty payload, an uninstalled provider
package, a call that raises, a search that finds nothing: all produce a valid
`ErrorSummary` with the deterministic findings intact and the reason recorded in
`investigation_notes`, while the sibling branches and the downstream nodes
keep running. Counts, templates and timings are exactly as accurate as they were
before the call failed — only the interpretation is missing.

That resilience costs visibility: a degraded run and a healthy one return the
same shape. The module therefore logs the whole pass — entry conditions,
fingerprinting output, prompt size, and the verdicts that came back — with the
degradation path logged at `ERROR` with a full traceback, since the note is a
summary and not a diagnosis.

---

## Web Search Node

**Module:** `graph_library/web_search/node.py` · **Entry point:** `web_search_node(state)`

The Web Search node is the optional detour between the Error Analysis node's two
passes. It exists because the model knows the common failures cold and the rare
ones not at all: a connection refused needs no research; a framework-internal
panic code that appears in one vendor's changelog and nowhere else does.

**Reads:** `search_queries`
**Writes:** `search_context`, `investigation_notes`, `completed_stages`

It is **off by default**. `enable_web_search` is opt-in because the capability
trades latency, cost and determinism for coverage of unfamiliar errors, and that
is the caller's trade to make, not the graph's. With the flag off the node never
runs and the Error Analysis node behaves exactly as it did before the capability
existed — no extra call, no extra latency, same output.

### The two-pass loop

1. **Pass 1 (Decision).** With the flag on and `search_context` still `None`, the
   Error Analysis node makes one cheap call showing the model only the signature
   templates, severities and loggers — the question is "have I heard of this?",
   which the wording answers; counts and timings are evidence for *causation* and
   belong to pass 2. The prompt is written to make *no search* the easy answer:
   an empty `queries` list is the expected, normal reply for connection refusals,
   timeouts, OOMs, null dereferences, HTTP 4xx/5xx and the standard exceptions of
   mainstream frameworks. At most **3** queries may be requested, and the cap is
   enforced in code rather than left to the schema description.
2. **The detour.** When queries come back, the node returns *only*
   `search_queries` — no summary, no `completed_stages`, because it has not
   finished — and `route_after_error_analysis` sends state to `web_search`.
3. **Pass 2 (Enrichment).** `web_search` writes `search_context` and routes back.
   The Error Analysis node runs its normal batched analysis with the snippets
   folded into the prompt, then fans into the downstream nodes.

The two passes are the same function, distinguished only by whether
`search_context` is `None` (undecided) or a list (decided, possibly empty). The
distinction between those two falsy values is load-bearing and bounds the loop to
a single lap: `web_search` writes a list on *every* path including every failure
path, so the router never sees `None` twice. A node that left the field unset on
failure would search, fail, and be routed straight back into another search.

The `recommendation` node is registered with `defer=True` for the same reason —
without it the join fires as soon as the plain edges have been written and
`recommendation` would run twice, once on an incomplete state.

### Retrieval and relevance

`graph_library/web_search/client.py` is the only module that talks to Tavily. Queries run at
`search_depth="basic"` (the node wants the summary paragraph off a documentation
page, not a thorough crawl), 3 results per query, a 20-second per-query timeout,
and snippets truncated to 500 characters.

The credential comes from `TAVILY_API_KEY` in the environment, never from graph
state. The SDK is imported lazily, so a deployment that never enables web search
does not have to install `tavily-python`.

**A relevance floor is enforced in the client, not in the prompt.** Tavily always
returns results — an invented error code still comes back with three pages about
unrelated memory errors, scoring 0.12–0.35 where a genuine match scores 0.6 and
above. `MIN_RELEVANCE_SCORE = 0.4` sits in the empty band between them. There is
deliberately no "keep the best one anyway" fallback: the best of three irrelevant
pages is still irrelevant, and this floor is the only thing standing between a
hallucinated error code and a confidently wrong explanation sourced from a
Windows bluescreen guide. A result carrying no score at all is kept — the floor
exists to reject results Tavily itself rates poorly, and absence of a rating is
not a poor one.

Each surviving result is rendered as `[query: …]` / `title — url` / content. The
query is carried into the snippet because the model reads several snippets from
several queries at once, and which question a page was answering is what tells it
which signature the page is about. The URL is included for the same reason a
citation is.

In the prompt, snippets are framed as *reference material* rather than as
evidence and fenced off from the signatures, because the two have very different
standing: the signatures are what happened, the snippets are pages a search
engine thought were related. The model is explicitly told it may discard them. A
node that fetches documentation and then implies the model must use it has only
moved the hallucination one step upstream.

### Failure handling

Failure is expected rather than exceptional here, because this is the one node
that depends on the public internet and on a credential the operator may simply
not have set. Queries are independent, so one failing does not cancel the others;
only a failure to build the client at all — no package, no credential — stops
everything, because it would stop every query identically. Each failure mode
produces an empty context, a note saying which, and a graph that keeps running.
The investigation is worse off by exactly the documentation it could not fetch,
and no more.

---

## Web Search Node Benchmark

Six end-to-end runs across four LLM providers, exercising both the two-pass loop
and the opt-out flag.

### Benchmark definitions

**Virtual Buffer Overflow Snippet (Inline)** is a two-line Pino JSON payload
built around a fabricated, deliberately niche infrastructure error code:

```json
{"level":50,"time":1722250761422,"name":"vbuf","msg":"ERR_X99_VIRTUAL_BUFFER_OVERFLOW_MEM_LOCK: virtual buffer lock could not be acquired"}
{"level":50,"time":1722250761423,"name":"api","msg":"Request 8f2c failed: upstream error"}
```

**`java_spring_boot_large.json.log`** is the large Spring Boot order-service
dataset from `sample_logs/` — thousands of lines of ordinary application business
errors (payment declined, out of stock, invalid order, shipping delay).

### Reference table

| Test | LLM Provider | Dataset / Source | Log Type | `enable_web_search` | Decision Pass Outcome | Web Node Executed? | Graph Routing Path |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Test 1 | DeepSeek | Virtual Buffer Overflow Snippet (Inline) | Niche Infrastructure | `true` | Emitted search query | Yes | error_analysis → web_search → error_analysis → recommendation |
| Test 2 | DeepSeek | Virtual Buffer Overflow Snippet (Inline) | Niche Infrastructure | `false` | Bypassed by flag | No | error_analysis → recommendation |
| Test 3 | OpenAI | Virtual Buffer Overflow Snippet (Inline) | Niche Infrastructure | `true` | Declined search (`[]`) | No | error_analysis → recommendation |
| Test 4 | Gemini | Virtual Buffer Overflow Snippet (Inline) | Niche Infrastructure | `true` | Declined search (`[]`) | No | error_analysis → recommendation |
| Test 5 | Anthropic | Virtual Buffer Overflow Snippet (Inline) | Niche Infrastructure | `true` | Declined search (`[]`) | No | error_analysis → recommendation |
| Test 6 | DeepSeek | `java_spring_boot_large.json.log` | Application Business Errors | `true` | Declined search (`[]`) | No | error_analysis → recommendation |

### Key findings

- **Two-Pass Loop.** When search is triggered (Test 1), `error_analysis` executes
  Pass 1 (Decision), routes state to `web_search` to fetch Tavily snippets, and
  returns to `error_analysis` Pass 2 (Enrichment) before fanning into downstream
  nodes.
- **Opt-Out Flag.** Setting `enable_web_search: false` cleanly bypasses both the
  decision call and the search node (Test 2).
- **Provider Discrimination.** High-capacity frontier models (OpenAI, Gemini,
  Anthropic) evaluate log semantics in Pass 1 and autonomously skip external
  search if internal parametric knowledge is sufficient (Tests 3–5).
- **Log Domain Intelligence.** Even when using a provider that requested search
  for niche errors (DeepSeek), standard domain-level business errors in large
  datasets are recognized as self-contained, bypassing search to save cost and
  latency (Test 6).

---

## Pattern Analysis & Multi-Provider Verification Matrix

The `pattern_analysis` and `error_analysis` nodes have been manually validated using `langgraph dev` across 12 distinct test configurations spanning 4 LLM providers (OpenAI, Anthropic, DeepSeek, Gemini), 3 reasoning modes (`fast`, `standard`, `deep`), and 3 representative log datasets.

### Verification Results Summary (12/12 Passed)

| Test ID | Application | Provider | Mode | Target Log File | Status | Core Verification Highlights |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Test 1** | `test1` | OpenAI | `fast` | `typescript_pino_recovery.log` | PASS | Fast cascade detection (`payment` -> `hotel`) |
| **Test 2** | `test2` | OpenAI | `standard` | `typescript_pino_recovery.log` | PASS | Structured anomaly classification (`logger_cascade`, `metadata_clustering`) |
| **Test 3** | `test3` | OpenAI | `deep` | `java_spring_boot_large.json.log` | PASS | Clean baseline execution (7.7k logs, 0 errors, single logger) |
| **Test 4** | `test4` | Anthropic | `fast` | `fastapi_recovery.log` | PASS | `volume_spike` onset & single-client endpoint correlation |
| **Test 5** | `test5` | Anthropic | `standard` | `typescript_pino_recovery.log` | PASS | Step-function transient fault synthesis (503 / 409 code clustering) |
| **Test 6** | `test6` | Anthropic | `deep` | `typescript_pino_recovery.log` | PASS | Envelope unwrapping (`_ToolArgumentEnvelope`) & deep root cause synthesis |
| **Test 7** | `test7` | DeepSeek | `fast` | `typescript_pino_recovery.log` | PASS | Blast radius evaluation (`ERR_003` mapping to 90 downstream failures) |
| **Test 8** | `test8` | DeepSeek | `standard` | `typescript_pino_recovery.log` | PASS | In-flight request retry tracking & scenario transition correlation |
| **Test 9** | `test9` | DeepSeek | `deep` | `fastapi_recovery.log` | PASS | Two-bucket plateau analysis & startup signature isolation |
| **Test 10** | `test10` | Gemini | `fast` | `fastapi_recovery.log` | PASS | Fast memory allocation failure synthesis (`sentiment-v1`) |
| **Test 11** | `test11` | Gemini | `standard` | `typescript_pino_recovery.log` | PASS | Cross-component bottleneck & rapid recovery synthesis |
| **Test 12** | `test12` | Gemini | `deep` | `java_spring_boot_large.json.log` | PASS | Synthetic benchmark load test recognition (7.7k logs/sec, 0 errors) |

### Key Architectural Validations Confirmed by Tests:
1. **Schema Resilience**: Tool argument unwrapping (`_ToolArgumentEnvelope`) successfully handles wrapped JSON payloads across Anthropic, DeepSeek, and Gemini structured output handlers.
2. **Graceful Degradation**: Fallback boundaries execute cleanly under schema errors or model timeouts without crashing graph execution threads.
3. **Multi-Mode Scaling**: `fast` mode generates concise executive summaries; `standard` and `deep` modes generate detailed JSON anomaly vectors and root-cause timelines.

---

## Local LLM Mock Testing

Use the local mock server to test graph execution and structured outputs without
calling external LLM APIs. `tests/mock_local_llm.py` is a minimal
OpenAI-compatible server: it speaks the chat-completions protocol over both
transports (plain JSON and `text/event-stream`), routes on the response schema
each node binds, and derives its answer from the prompt it was sent rather than
returning a canned blob. No API key, no network, no GPU.

Both LLM nodes reach it in a single run. `llm_provider` is read from graph state
by every LLM node, so one setting points `error_analysis` and `pattern_analysis`
at the same endpoint, where they ask for different schemas —
`LLMErrorAnalysisResult` and `LLMSearchDecision` for the first,
`PatternAnalysisResult` for the second.

### 1. Verify `.env` settings

```bash
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080/v1
LOCAL_LLM_MODEL_NAME=mock-local-llm
LOCAL_LLM_API_KEY=cant-be-empty
```

The port in `LOCAL_LLM_BASE_URL` must match the port the server is started on in
step 2. A mismatch fails as `openai.APIConnectionError: Connection error` from
both LLM nodes while the mock's terminal logs nothing at all — the client is
knocking on a closed port, so there is no request for the server to report. The
key is a placeholder the mock ignores; it only has to be non-empty, because the
OpenAI client refuses to send a blank one. `langgraph.json` already loads `.env`
via `"env": ".env"`, and reads it at startup — restart `langgraph dev` after
editing.

### 2. Start the mock server (terminal 1)

```bash
python3 -m uvicorn tests.mock_local_llm:app --port 8080
```

### 3. Launch LangGraph dev (terminal 2)

```bash
langgraph dev
```

### 4. Run in the LangGraph Studio UI

| Input | Value |
| :--- | :--- |
| `application_name` | `test1` |
| `llm_provider` | `local` |
| `enable_web_search` | `true` |
| `raw_logs` | contents of `sample_logs/typescript_pino_recovery.log` |

`raw_logs` is the log text itself, not a path — paste the file's contents into
the field.

### Expected results

**Mock server (terminal 1)** — three `200 OK` responses, one per structured call:

```
INFO:     127.0.0.1:50677 - "POST /v1/chat/completions HTTP/1.1" 200 OK
INFO:     127.0.0.1:50677 - "POST /v1/chat/completions HTTP/1.1" 200 OK
INFO:     127.0.0.1:50677 - "POST /v1/chat/completions HTTP/1.1" 200 OK
```

One is the error-analysis search-decision pass, one the error-analysis
root-cause pass, one the pattern-analysis call.

**Graph output** — both LLM nodes complete against their own schemas, with no
degradation notes:

- `error_summary` carries 3 signatures with `primary_error_signature_id` set and
  every signature's `explanation` filled in;
- `pattern_summary` carries anomalies across all four categories —
  `volume_spike`, `baseline_shift`, `logger_cascade`, `metadata_clustering`;
- `completed_stages` reaches `report_generator`;
- `investigation_notes` contains no `LLM reasoning unavailable` entry. That
  absence is the assertion that matters: both nodes degrade rather than fail, so
  a connection error produces a complete-looking run whose interpretation is
  silently missing, and the note is the only place it shows.

**On `enable_web_search`** — the flag is honoured, but no search runs and no
Tavily credential is needed. The mock answers the decision pass with an empty
`queries` list, which is the expected answer for ordinary errors and is what
keeps a local run local: a query here would be served by Tavily over the real
network, not by this server. The router therefore reads `search_queries` as
empty and goes straight to `recommendation`, leaving `search_context` as `[]` —
decided, with nothing to show for it. To exercise the retrieval loop itself, use
a real provider.

**On the mock's answers** — they are derived, not canned, and deliberately
distinguishable from the deterministic fallbacks. Error analysis nominates the
last signature in the batch (the lowest-volume one) as the root cause; pattern
analysis decodes the STATISTICS and TIMELINE blocks back out of the prompt and
reports the real loggers, timestamps and metadata values it was sent, opening
its `behavioral_synthesis` with `Mock local model:`. If a summary instead opens
with `Deterministic summary`, the model was never reached and the node fell back
to arithmetic.

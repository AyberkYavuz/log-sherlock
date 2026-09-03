# LogSherlock Graph Nodes

LogSherlock is a log analysis platform built as a LangGraph workflow. Every node
of that graph is **fully implemented today**:

- **Parser** — turns raw log text into normalized entries
- **Statistics** — reports what the parsed dataset contains
- **Timeline** — reports how the incident unfolded over time
- **Pattern Analysis** — reads those two reports and says what about them is abnormal
- **Error Analysis** — groups the errors and explains which one started it
- **Web Search** — optionally fetches external documentation for obscure errors
- **Prepare Output** — fans every finding in, scores it, and serializes the report
- **Write to DB** — persists that report to PostgreSQL

Each node is described from its Python source. `graph.py` now defines no node of
its own: every stage lives in its own feature package under `graph_library/` and
is registered there.

---

## How the Nodes Fit Together

Every node has the same signature: it accepts the full graph state and returns a
*partial* state delta containing only the keys it owns. No node mutates state in
place.

The topology is fixed. `parser` fans out into three parallel branches, and every
analysis stage fans back in to `prepare_output`:

```
START
  -> parser -----------------------------------------------------------------------+
  -> [ error_analysis (LLM) <-> web_search (network) ] ---------------------------+|
  -> [ statistics (deterministic), timeline (deterministic) ]                     ||
         |                                       |                                ||
         +---------------------------------------+-> pattern_analysis (LLM) ------++-> prepare_output -> write_to_db -> END
         |                                                                        |
         +------------------------------------------------------------------------+
```

Three things about that shape are worth stating plainly:

- **`pattern_analysis` is downstream of the deterministic pair, not parallel to
  it.** The patterns it looks for are properties of `statistics` and `timeline`
  output — the distributions one produces, the buckets and milestones the other
  does — so it consumes both rather than re-reading `parsed_logs`. Two plain
  edges into one node is a join: it runs once, after *both* have landed.
- **`prepare_output` takes a direct edge from all four analysis stages.** It
  needs `statistics` and `timeline` in raw form as well as the patterns derived
  from them, so those two feed it directly in addition to feeding
  `pattern_analysis`. `error_analysis` is the exception only in mechanism: it
  arrives via `route_after_error_analysis` rather than a plain edge, because the
  same branch point also owns the web-search detour.
- **`prepare_output` also takes a direct edge from `parser`.** That fourth edge
  out of `parser` carries `parser_metrics`, which reaches the synthesis no other
  way: every analysis stage publishes its own artifact rather than forwarding
  its inputs, so `statistics` deliberately omits parser health and `timeline`
  reads the metrics without republishing them. The edge is what lets a
  conclusion be *qualified* rather than merely stated — a root cause inferred
  from a payload where a third of the lines were malformed, or where most
  entries carried no timestamp, warrants a lower confidence score and an
  explicit data-quality caveat in the report. Those metrics are the input to the
  deterministic scoring engine documented under
  [Prepare Output Node](#prepare-output-node).

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

The `prepare_output` node is registered with `defer=True` for the same reason —
without it the join fires as soon as the plain edges have been written and
`prepare_output` would run twice, once on an incomplete state.

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

## Prepare Output Node

**Module:** `graph_library/prepare_output/node.py` · **Entry point:** `prepare_output_node(state)`

The Prepare Output node answers *"so what actually happened, and how much should
anyone trust that answer?"* It is the graph's fan-in: the first and only node
that sees every upstream artifact at once, reached by a plain edge from
`statistics`, `timeline` and `pattern_analysis`, by the conditional branch out of
`error_analysis`, and by the fourth edge out of `parser` that carries
`parser_metrics`. It is registered with `defer=True`, which is what holds it
until every one of those branches has landed — see
[How the Nodes Fit Together](#how-the-nodes-fit-together).

**Reads:** `parser_metrics`, `statistics`, `timeline`, `error_summary`,
`pattern_summary`, `investigation_notes`, `historical_context`,
`application_name`, `investigation_timestamp`, `llm_provider`, `analysis_mode`
**Writes:** `root_cause`, `executive_summary`, `confidence_score`,
`structured_report`, `completed_stages`, and `investigation_notes` when there is
something to record

The node has two responsibilities, and they are worth separating because they
serve different consumers:

- **Synthesis for humans.** One batched LLM call turns five payloads into a
  one-sentence `root_cause` and a multi-paragraph `executive_summary`, published
  alongside a `confidence_score` that is *not* asked of the model. These are the
  fields a dashboard headline and an alert body are built from.
- **Serialization for machines.** Every upstream artifact is assembled verbatim
  into `structured_report`, the composite artifact a UI hydrates from and the
  `write_to_db` node persists to PostgreSQL. Nothing is summarized on the way
  in: a client that wants to draw the timeline needs the timeline, not a
  sentence about it.

The order matters. The confidence score is computed *before* anything is asked of
a model, because it is the one number in the report that must not depend on
whether the call succeeded.

### The deterministic pass — confidence scoring

`graph_library/prepare_output/scoring.py` computes `confidence_score` by
arithmetic over parser health and the error analysis. Asking the model instead
would measure the wrong thing: a model rates the clarity of the signal it was
shown and has no way to know that a third of the payload never reached it. The
four penalties below are exactly the things a model cannot see.

| Rule | Deduction | Source |
| --- | --- | --- |
| Base score | starts at `100` | — |
| Malformed lines | −1 pt per 1% | `malformed_lines / total_lines` |
| Missing timestamps | −1 pt per 2% | `missing_timestamp_lines / parsed_lines` |
| Root-cause ambiguity | −15 pts | `error_summary.primary_error_signature_id` is `None` |
| Low parser confidence | −10 pts | `parser_metrics.parser_confidence < 0.80` |
| Synthesis fallback | −10 pts | applied when the LLM pass did not answer |
| Bounds | clamped to `[0, 100]` | `max(0, min(100, score))` |

Four details are load-bearing:

- **The two ratio penalties carry different weights on purpose.** A malformed
  line contributed *nothing* to any downstream analysis, so it is pure missing
  evidence at 1 point per percent. An entry with no timestamp still reached the
  statistics and the error fingerprinting and only dropped out of the timeline —
  partial evidence rather than absent — so it costs half as much.
- **Ambiguity is the largest single penalty** because a root-cause statement with
  no root-cause candidate behind it is the output most likely to read more
  certain than it is.
- **Rounding happens once, at the end.** Two sub-point penalties therefore cost a
  point together rather than nothing each — 1 malformed line and 2 missing
  timestamps out of 300 is 0.33 + 0.33 points, and rounding either alone would
  discard it.
- **An unmeasurable payload is not a bad one.** A zero or missing denominator
  yields no ratio penalty rather than a maximal one; "we could not tell" is not
  evidence of poor quality, and the empty analysis such a payload produces is
  already caught by the ambiguity penalty. A `parser_confidence` of exactly
  `0.80` is good enough — the penalty applies strictly below it — while a genuine
  `0.0` is penalized rather than misread as "not reported".

Every weight is a named module constant, so the whole policy is readable at a
glance and tuning it is a one-line edit. `confidence_breakdown(state)` returns
the same figures per penalty and is what the node logs, because a report that
scored 61 raises "why?" and a single number cannot answer it.

### The LLM pass — synthesis

`graph_library/prepare_output/prompts.py` assembles seven sections into one turn,
ordered from measurement to inference: `PARSER HEALTH`, `STATISTICS`, `TIMELINE`,
`ERROR ANALYSIS`, `PATTERN ANALYSIS`, `INVESTIGATION NOTES`, `HISTORICAL
CONTEXT`. The statistics, timeline and notes sections are the ones the Pattern
Analysis node already renders, imported from its public surface rather than
reimplemented, so the two nodes describe the same payload identically and remain
comparable when their conclusions differ.

Three decisions shape what gets sent:

- **Provenance is stated section by section.** The error and pattern summaries
  are themselves model output, and a synthesis that treats another model's
  inference as a measured fact compounds the first model's error. Both sections
  are labelled as another model's conclusions, and the system prompt tells the
  model it may disagree with them and say why. Reading the facts *before* the
  interpretations is what makes that possible, which is why the ordering above is
  not cosmetic.
- **Parser health is included; the confidence score is not.** The metrics are
  what the summary's data-quality caveat has to be built from. The deterministic
  score derived from those same metrics is deliberately withheld, so that the
  model's own rating stays an independent reading rather than an echo.
- **The bounded inputs state their own omissions.** Signatures are capped at
  `MAX_ERROR_SIGNATURES` (12) of the count-ordered batch and historical
  investigations at `MAX_HISTORICAL_INVESTIGATIONS` (3), each with the omission
  written into the prompt. `sample_messages` are *not* resent with the
  signatures: the Error Analysis node has already read them and published an
  `explanation` per signature, so repeating them is the largest avoidable cost
  in this prompt.

The system prompt is written against the three failure modes a model shows when
handed a complete investigation: restating the inputs instead of concluding from
them, promoting the loudest signature to the cause when it is frequently
downstream noise, and writing with uniform certainty regardless of how much of
the payload was readable. It requires the executive summary to cover, in order,
what happened and when, how the failure spread, and what limits the conclusion.

The response schema is `LLMPrepareOutputResult`, bound as a structured tool call
and inheriting `_ToolArgumentEnvelope` like every other response schema in
`graph_library/models/` — some providers nest the tool-call arguments one level
deeper than the schema they were given, and a schema whose fields are all
required would otherwise fail with three "field required" errors against a
payload that actually contained them.

| Field | Meaning |
| --- | --- |
| `root_cause` | One sentence naming the core trigger |
| `executive_summary` | The multi-paragraph narrative |
| `llm_confidence_score` | The model's own 0–100 rating of signal clarity |

**`llm_confidence_score` never becomes the published `confidence_score`.** The
published value is always the deterministic one. The model's self-assessment is
logged, and a gap wider than `CONFIDENCE_DIVERGENCE_THRESHOLD` (25 points) is
written to `investigation_notes` — the two measure different things, so a gap is
expected and only a wide one is informative. A model reporting 95 on a payload
scored 55 has read a clean signal out of an incomplete dataset, and that is worth
a sentence in the investigation.

### Output — `StructuredInvestigationReport`

The report is partitioned by **provenance** rather than by topic, and that is the
whole design: a reader of a stored investigation can always tell which numbers
are facts and which sentences are inferences, without having to know which node
produced what. That distinction is exactly what gets lost when a report is
flattened into one bag of fields.

| Section | Contents | Provenance |
| --- | --- | --- |
| `metadata` | `application_name`, `investigation_timestamp`, `analysis_mode`, `llm_provider`, `confidence_score`, `parser_metrics` | Run identity and ingestion health |
| `synthesis` | `root_cause`, `executive_summary`, `investigation_notes` | This node's own conclusions |
| `deterministic_outputs` | `statistics`, `timeline` | Arithmetic — reproducible from the same logs |
| `ai_insights` | `error_summary`, `pattern_summary` | The two upstream LLM nodes' conclusions |

```json
{
  "metadata": {
    "application_name": "checkout-api",
    "investigation_timestamp": "2026-01-01T11:00:00+00:00",
    "analysis_mode": "standard",
    "llm_provider": "openai",
    "confidence_score": 90,
    "parser_metrics": { "detected_format": "json", "total_lines": 100, "...": "..." }
  },
  "synthesis": {
    "root_cause": "The payment client lost its database connection, which stalled every downstream order.",
    "executive_summary": "At 10:05 the payment client began refusing connections...",
    "investigation_notes": ["Parser: 3 malformed lines were skipped."]
  },
  "deterministic_outputs": {
    "statistics": { "severity": { "...": "..." }, "...": "..." },
    "timeline": [{ "event_type": "milestone", "milestone_kind": "first_error", "...": "..." }]
  },
  "ai_insights": {
    "error_summary": { "primary_error_signature_id": "ERR_001", "...": "..." },
    "pattern_summary": { "anomalies": [{ "...": "..." }], "...": "..." }
  }
}
```

Two notes on the fields:

- `metadata.analysis_mode` and `metadata.llm_provider` record the *normalized*
  values — the run's reproducibility record, so `anthropic` is what appears when
  the caller typed `Claude`. `metadata.confidence_score` is the same value as the
  `confidence_score` state field, carried inside the report so a persisted row is
  self-contained.
- `synthesis.investigation_notes` is a snapshot of the *upstream* notes as they
  stood when the report was built, copied rather than aliased so the graph's
  additive reducer cannot mutate a report that has already been assembled. This
  node's own notes — a degradation reason, a substituted model, a wide confidence
  gap — reach the graph's `investigation_notes` channel instead, and so appear in
  the final state rather than in this snapshot.

### Degradation

The node degrades rather than fails, and here that guarantee is stronger than
elsewhere in the graph: this is the node that produces the artifact everything
downstream persists, so it must publish a complete `structured_report` on every
path. Four failure modes are caught, all of them into the same fallback:

- **A call that raises or times out** — no credential, an unreachable endpoint, a
  rate limit, a reset connection.
- **A provider package that is not installed** — the `ImportError` from the lazy
  SDK import in `llm_factory`.
- **A response that does not satisfy the schema** — validation happens *inside*
  the guard, not after it, so a `ValidationError` against a well-formed but wrong
  payload takes the fallback path instead of escaping the one node that must
  always publish.
- **An entirely empty investigation** — no statistics, no timeline, no error
  signatures and no patterns. The call is skipped rather than spending a request
  to be told the input was empty, and `NO_INPUT_NOTE` says so verbatim, so a
  reader can tell "nothing went wrong" from "there was nothing to look at".

On every one of those paths the node publishes `FALLBACK_ROOT_CAUSE` and
`FALLBACK_EXECUTIVE_SUMMARY` — fixed text worded to be unmistakably an *absence*
rather than a finding, so a UI showing it is showing the truth about the run —
records the reason in `investigation_notes`, and applies the additional −10
`FALLBACK_PENALTY` to the score. Every deterministic number in a degraded report
is exactly as accurate as it would have been in a healthy one; what is missing is
the reasoning that connects them, and the discounted score is what says so.

Because both paths build the report through the same assembly step, a degraded
run and a healthy one publish an identical shape — the difference shows up in the
synthesis text, the confidence score and the notes, never in the schema, so no
downstream consumer has to ask which one it got. Upstream artifacts are read
defensively even though the fan-in guarantees every producer has run: a missing
section is recoverable, and a `KeyError` here would lose an entire investigation
that was otherwise complete.

### Model selection

The node reuses `graph_library/error_analysis/llm_factory.py` wholesale, exactly
as the Pattern Analysis node does — the same five providers, the same three
tiers, the same `(provider, mode)` routing, the same `MODEL_FALLBACKS` chain and
the same `is_model_unavailable` classification. Only a model-identity failure is
retried; an expired key, a rate limit or a timeout is raised straight through and
degrades, because swapping the model would burn another request and fail the same
way. A substitution is always recorded in `investigation_notes`, since a silent
one would make the report unreproducible.

### The report survives to the final state

Worth stating because it was once not true. The `write_to_db` stub that
preceded the implemented node returned `"structured_report": {}`; it ran
immediately after this node, the field has no reducer, and the later write won,
so the final state's report was an empty dict even though this node had
published a complete one.

The implemented node returns only the three fields it owns — `db_persisted`,
its investigation note and its `completed_stages` entry — so the report now
reaches the final state intact:

```python
report = compile_graph().invoke(inputs)["structured_report"]
```

Per-node streaming still works and is still the way to observe *when* the report
was assembled rather than merely what it contains:

```python
for update in compile_graph().stream(inputs, stream_mode="updates"):
    if "prepare_output" in update:
        report = update["prepare_output"]["structured_report"]
```

---

## Write to DB Node

**Module:** `graph_library/write_to_db/node.py` · **Entry point:** `write_to_db_node(state)`

The last node in the topology and the only one that leaves the process. It takes
the `structured_report` the Prepare Output node assembled and writes it to a
PostgreSQL `investigations` table, keyed by `investigation_id`.

**Reads:** `structured_report`, `investigation_id`, and `application_name`,
`confidence_score`, `analysis_mode`, `llm_provider` as fallbacks
**Writes:** `db_persisted`, `investigation_notes`, `completed_stages`

It contributes no analysis and publishes none. Three fields leave it, and the
absence of a fourth is deliberate: it does not return `structured_report`. A
node that does not own a field must not return it — see
[The report survives to the final state](#the-report-survives-to-the-final-state).

### Package layout

The feature package is split the way its siblings are, by concern:

- `config.py` — `DatabaseConfig`, read from the environment
- `queries.py` — every SQL statement, in one reviewable place
- `db.py` — connection handling and the two operations built on it
- `node.py` — the graph node

Two consumers share that surface, which is why the operations are functions in
`db.py` rather than methods on the node: `graph.py` imports `write_to_db_node`,
and the root `init_db.py` imports `initialize_database` and `DatabaseConfig`. The
schema is therefore declared once and the script that creates the table cannot
drift from the node that writes to it.

`psycopg2` is imported **lazily**, inside the functions that use it. This package
is reachable from `graph.py` through the node registry, so a module-scope import
would make the driver a hard requirement of *building* the graph: a deployment
that never persists anything would fail to start rather than simply never call
this node.

### Connection settings

Credentials come from the environment and never from graph state, for the same
reason the LLM provider keys do — a value that lives in state can end up in a
checkpoint, a LangSmith trace or a persisted report. Six variables, each with a
default applied when it is absent *or empty*, so a half-filled `.env` behaves
like a missing one:

- `DB_HOST` — defaults to `localhost`
- `DB_PORT` — defaults to `5432`
- `DB_NAME` — defaults to `postgres`
- `DB_USER` — defaults to `postgres`
- `DB_PASSWORD` — no default; an empty password is *omitted* from the connection
  parameters rather than sent as `""`, because the two are not the same to libpq
  and a blank one is rejected by a trust-authenticated server
- `DB_CONNECT_TIMEOUT` — defaults to `5` seconds, short on purpose: this node
  runs at the end of a graph run, so an unreachable database must degrade in
  seconds rather than hang a run that has already produced its whole report

The node does **not** load `.env` itself, and that is a correctness requirement
rather than a style choice. `load_dotenv` mutates `os.environ` for the whole
process, so a node calling it would inject every key in the file — provider
credentials, `LANGSMITH_TRACING` — into a process that had deliberately not set
them. Under a test suite that is the difference between a hermetic run and one
that makes real, billed API calls from the next node that looks for a key.
Populating the environment belongs to the entry point: `langgraph.json` does it
with `"env": ".env"`, and `init_db.py` calls `load_env_file()` explicitly.

### The `investigations` table

Created by `init_db.py`, never by the node. A node that issued DDL would need
elevated privileges on every run, and a typo in a report would become a schema
migration.

```sql
CREATE TABLE IF NOT EXISTS investigations (
    investigation_id  VARCHAR(255) PRIMARY KEY,
    application_name  VARCHAR(255),
    confidence_score  INTEGER,
    analysis_mode     VARCHAR(50),
    llm_provider      VARCHAR(50),
    structured_report JSONB,
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
```

Three choices in that schema are load-bearing:

- **`investigation_id` is the caller's identifier, not a generated surrogate
  key.** That is what makes the write idempotent: re-running an investigation
  under the same id corrects the stored row rather than accumulating a second
  one.
- **`structured_report` is `JSONB`, not `JSON`.** It is queried far more than it
  is round-tripped, and only `JSONB` can be indexed. A stored report answers
  `#>> '{synthesis,root_cause}'` or `jsonb_array_length(... #> '{deterministic_outputs,timeline}')`
  in place, without being deserialized first.
- **The four columns beside it duplicate values that also live inside that
  document.** They are what a dashboard filters and sorts on, and neither copy
  is authoritative over the other because both are written from the same report
  in one statement.

`confidence_score` is stored as SQL `NULL` rather than `0` when a run produced
none, so "not measured" and "measured as zero" stay distinguishable.

### `investigation_id` — supplied or generated

The id is **optional**. When the caller supplies none — absent, `null`, empty or
whitespace — the node generates one in the form `inv-graph-<8 hex chars>` from
`uuid.uuid4()`, and records the fact in `investigation_notes`:

> Write to DB: no investigation_id was supplied, so `inv-graph-3f9a2c41` was
> generated for this run. Supply an id in the input state to make re-runs update
> the same row instead of storing a new one.

The run then persists normally and returns `db_persisted: True`. Generating
rather than refusing is what makes persistence the default: a LangGraph Studio
run has no natural place to type a primary key, and a complete investigation
that goes unstored because of that is the worse outcome.

The trade-off is real and is the reason the note exists. **A generated id is not
idempotent across runs** — re-running the same logs mints a new key and stores a
second row, where a caller-supplied id would have corrected the first. The
`inv-graph-` prefix is visible on purpose, so an operator reading a stored row
can tell a key their system chose (and can therefore correlate with something)
from one this run invented (which correlates with nothing outside the table).

An id the caller *did* supply is never replaced. One longer than 255 characters
is reported as the input problem it is and the run is not persisted, rather than
being silently truncated or swapped for a generated one.

### The write

One idempotent statement rather than a select-then-branch, which would have a
race between two graph runs finishing at once and would be three round trips
where this is one:

```sql
INSERT INTO investigations (
    investigation_id, application_name, confidence_score,
    analysis_mode, llm_provider, structured_report, updated_at
)
VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
ON CONFLICT (investigation_id) DO UPDATE SET
    application_name  = EXCLUDED.application_name,
    confidence_score  = EXCLUDED.confidence_score,
    analysis_mode     = EXCLUDED.analysis_mode,
    llm_provider      = EXCLUDED.llm_provider,
    structured_report = EXCLUDED.structured_report,
    updated_at        = CURRENT_TIMESTAMP;
```

`created_at` appears in neither half by design: it keeps its column default on
the first write and is untouched by every later one, so the row remembers when
the investigation was first stored even after it is re-run. `updated_at` is set
from `CURRENT_TIMESTAMP` in the update branch rather than from
`EXCLUDED.updated_at`, so the stored time is the server's and not one derived
from whatever clock the graph ran on.

The four relational values are looked up in `structured_report["metadata"]`
first, then `structured_report["synthesis"]`, then the top level of graph state.
Metadata is where Prepare Output records the run's identity and holds the
*normalized* provider and mode — `anthropic` where the caller typed `Claude` —
which is what makes the stored row a reproducibility record rather than a
transcript of the form.

### Execution visibility

Progress is written to the logger *and* to stdout with a `[LogSherlock DB]`
prefix, because LangGraph Server and the CLI show a node's stdout directly and a
persistence step that reports nothing there looks identical to one that never
ran:

```
[LogSherlock DB] Connecting to Postgres at localhost:5432/postgres as postgres...
[LogSherlock DB] Successfully persisted investigation inv-graph-3f9a2c41
```

The password has no representation in any of those lines. The log target is
built from a `host:port/dbname` property that cannot carry a credential, rather
than from a DSN with one substitution away from leaking.

### Degradation

The node degrades rather than fails, and the bar is higher here than anywhere
else in the graph. Every other node degrades to protect the run; this one
degrades to protect a run that is already *complete*. By the time it executes,
the payload has been parsed, analyzed, synthesized and scored, so an unreachable
database must cost the storage and nothing else.

Every failure path returns the same shape as the happy one, with
`db_persisted: False` and the reason in `investigation_notes`. Nothing raises out
of `write_to_db_node`. The paths are:

- **No `structured_report`** — nothing to store; caught before the driver is
  imported and before a socket is opened, so a run with nothing to persist does
  not spend a connection discovering that.
- **A supplied `investigation_id` over 255 characters** — checked locally rather
  than left to the server, so it reads as an input problem and not as a write
  that failed halfway.
- **An unreachable server, a rejected credential, a missing database, a missing
  driver** — caught, logged with a full traceback, and summarized in one line.
  The driver's message is collapsed to a single bounded line for the note, since
  `psycopg2` reports a refused connection over four lines and an investigation
  note is read by a human.

`completed_stages` gains `"write_to_db"` on **every** path, including the failure
paths. The channel records which stages ran, not which succeeded — the note and
the `db_persisted` flag are what carry the outcome — and a node that omitted
itself on failure would make a degraded run indistinguishable from a truncated
one.

### Database initialization — `init_db.py`

A root-level script, run once before a session of investigations, in either
deployment:

```bash
python3 init_db.py
```

Local development reads `.env`; a Docker Compose deployment sets the same `DB_*`
variables in the service environment and needs no file. One code path serves
both, because the only difference between them is what the values are.

In one connection it loads `.env` if there is one, connects, **truncates** the
`investigations` table if it exists, and **creates** it if it does not.
Create-or-truncate rather than drop-and-recreate: truncating leaves the column
types, the primary key and any index or grant a deployment has added exactly as
they were, where a drop would discard all of them silently and replace the table
with whatever the current release happens to declare.

**The script empties the table.** That is its purpose, but it means it is not
something to point at a database whose contents matter. It reports the target
and what it did on every run, so a mistake is visible in the output:

```
Target: localhost:5432/postgres (user postgres)
[LogSherlock DB] Connecting to Postgres at localhost:5432/postgres as postgres...
[LogSherlock DB] Table 'investigations' not found; creating it

OK: table 'investigations' created on localhost:5432/postgres
```

Exit codes are distinct because the fixes are: `0` success, `1` a connection or
statement failure (a deployment problem), `2` a missing driver (an install
problem, reported with the `pip install psycopg2-binary` command). Failures print
an actionable sentence rather than a traceback whose last frame is inside the
driver.

### A note on running the test suite

The suite runs the full graph end to end several times without supplying an
`investigation_id`. With a reachable PostgreSQL, each of those runs now generates
an id and stores a real row — roughly 17 per `pytest` invocation. That is the
auto-generation behaviour working as designed, not a fault, but it does mean the
suite writes to whatever database `DB_*` points at.

Point the tests somewhere harmless if that matters:

```bash
DB_HOST=127.0.0.1 DB_PORT=1 python3 -m pytest -q
```

Every write then fails fast, each run records its degradation note, and the
assertions are unaffected — they check that `write_to_db` ran, not that it
stored anything.

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
| Test 1 | DeepSeek | Virtual Buffer Overflow Snippet (Inline) | Niche Infrastructure | `true` | Emitted search query | Yes | error_analysis → web_search → error_analysis → prepare_output |
| Test 2 | DeepSeek | Virtual Buffer Overflow Snippet (Inline) | Niche Infrastructure | `false` | Bypassed by flag | No | error_analysis → prepare_output |
| Test 3 | OpenAI | Virtual Buffer Overflow Snippet (Inline) | Niche Infrastructure | `true` | Declined search (`[]`) | No | error_analysis → prepare_output |
| Test 4 | Gemini | Virtual Buffer Overflow Snippet (Inline) | Niche Infrastructure | `true` | Declined search (`[]`) | No | error_analysis → prepare_output |
| Test 5 | Anthropic | Virtual Buffer Overflow Snippet (Inline) | Niche Infrastructure | `true` | Declined search (`[]`) | No | error_analysis → prepare_output |
| Test 6 | DeepSeek | `java_spring_boot_large.json.log` | Application Business Errors | `true` | Declined search (`[]`) | No | error_analysis → prepare_output |

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

**All three LLM nodes reach it in a single run, across four schemas.**
`llm_provider` is read from graph state by all of them, so one setting points
`error_analysis`, `pattern_analysis` and `prepare_output` at the same endpoint,
where they ask for four different response schemas:

| Node | Schema | Pass |
| :--- | :--- | :--- |
| `error_analysis` | `LLMSearchDecision` | Pass 1 — search triage |
| `error_analysis` | `LLMErrorAnalysisResult` | Pass 2 — root cause |
| `pattern_analysis` | `PatternAnalysisResult` | Behavioral patterns |
| `prepare_output` | `LLMPrepareOutputResult` | Fan-in synthesis |

The server has a payload builder for every one of them, so **`prepare_output`
no longer takes its degradation path on a local mock run.** It receives a
payload its schema accepts and publishes derived `root_cause`,
`executive_summary` and `llm_confidence_score` values rather than
`FALLBACK_ROOT_CAUSE` / `FALLBACK_EXECUTIVE_SUMMARY` with a discounted score.
See [Expected results](#expected-results) for what a healthy run looks like and
[Verified manual run](#verified-manual-run) for one that was actually observed.

### 1. Verify `.env` settings

```bash
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080/v1
LOCAL_LLM_MODEL_NAME=mock-local-llm
LOCAL_LLM_API_KEY=cant-be-empty
```

The port in `LOCAL_LLM_BASE_URL` must match the port the server is started on in
step 2. A mismatch fails as `openai.APIConnectionError: Connection error` from
every LLM node while the mock's terminal logs nothing at all — the client is
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
| `application_name` | `mock_local_llm.py test 1` |
| `llm_provider` | `local` |
| `enable_web_search` | `true` |
| `raw_logs` | contents of `sample_logs/typescript_pino_recovery.log` |

`raw_logs` is the log text itself, not a path — paste the file's contents into
the field.

### Expected results

**Mock server (terminal 1)** — four `200 OK` responses, one per structured call:

```
INFO:     127.0.0.1:50677 - "POST /v1/chat/completions HTTP/1.1" 200 OK
INFO:     127.0.0.1:50677 - "POST /v1/chat/completions HTTP/1.1" 200 OK
INFO:     127.0.0.1:50677 - "POST /v1/chat/completions HTTP/1.1" 200 OK
INFO:     127.0.0.1:50677 - "POST /v1/chat/completions HTTP/1.1" 200 OK
```

One is the error-analysis search-decision pass, one the error-analysis
root-cause pass, one the pattern-analysis call, and one the prepare-output
synthesis call.

**Graph output** — all three LLM nodes complete against their own schemas, with
no degradation notes:

- `error_summary` carries 3 signatures with `primary_error_signature_id` set and
  every signature's `explanation` filled in;
- `pattern_summary` carries anomalies across all four categories —
  `volume_spike`, `baseline_shift`, `logger_cascade`, `metadata_clustering`;
- `root_cause` and `executive_summary` carry the mock's derived synthesis, not
  the fallback text — both open with `Mock local model:`;
- `confidence_score` is the deterministic score, undiscounted, because no
  fallback penalty was applied;
- `completed_stages` reaches `write_to_db`;
- `investigation_notes` contains no `LLM reasoning unavailable` and no
  `LLM synthesis unavailable` entry. That absence is the assertion that matters:
  every one of these nodes degrades rather than fails, so a connection error
  produces a complete-looking run whose interpretation is silently missing, and
  the note is the only place it shows.

**On `prepare_output` against this server** — the fourth call is answered with a
payload its schema accepts, so the node takes its healthy path end to end. The
mock keys `PAYLOAD_BUILDERS` on the schema name the client puts on the wire and
has a row for `LLMPrepareOutputResult`; where the request declares no name it
resolves on the schema's own `llm_confidence_score` field, and where it declares
neither it resolves on the `PARSER HEALTH` heading that only the synthesis
prompt carries. That last marker is checked *before* the `STATISTICS` one,
because `prepare_output` reuses the pattern-analysis formatters and therefore
carries that heading too — an ordering that is load-bearing rather than
cosmetic.

The fallback path is still reachable and still correct; it is simply no longer
what a local run exercises. To see it, point `LOCAL_LLM_BASE_URL` at a closed
port: the node publishes `FALLBACK_ROOT_CAUSE` / `FALLBACK_EXECUTIVE_SUMMARY`,
discounts the score by 10, and records the reason in `investigation_notes`.

**On `enable_web_search`** — the flag is honoured, but no search runs and no
Tavily credential is needed. The mock answers the decision pass with an empty
`queries` list, which is the expected answer for ordinary errors and is what
keeps a local run local: a query here would be served by Tavily over the real
network, not by this server. The router therefore reads `search_queries` as
empty and goes straight to `prepare_output`, leaving `search_context` as `[]` —
decided, with nothing to show for it. To exercise the retrieval loop itself, use
a real provider.

**On the mock's answers** — they are derived, not canned, and deliberately
distinguishable from the deterministic fallbacks. Error analysis nominates the
last signature in the batch (the lowest-volume one) as the root cause. Pattern
analysis decodes the STATISTICS and TIMELINE blocks back out of the prompt and
reports the real loggers, timestamps and metadata values it was sent. Prepare
output decodes all five sections of the synthesis prompt — PARSER HEALTH,
STATISTICS, TIMELINE, ERROR ANALYSIS and PATTERN ANALYSIS — and writes a root
cause naming the signature the error-analysis node actually nominated, a
three-paragraph summary in the order that node's own system prompt requires
(what happened and when, how the failure spread, what limits the conclusion),
and an `llm_confidence_score` derived from how clearly the evidence points at
one cause.

The two narrative fields — `behavioral_synthesis` and the prepare-output
`root_cause` / `executive_summary` — open with `Mock local model:`, and that
prefix is the assertion, because those are the fields whose deterministic
fallbacks are otherwise shaped identically. A `behavioral_synthesis` opening
with `Deterministic summary` means the pattern node fell back to arithmetic; a
`root_cause` reading `Root cause undetermined` means the synthesis call never
landed. Neither should appear on a healthy local run. The error-analysis fields
carry no such prefix and need none: they echo signature ids, which are checkable
against the batch directly.

### Verified manual run

The run below was executed through `langgraph dev` against the mock server and
is the reference for what a healthy four-call local run produces. Two terminals,
exactly as in steps 2 and 3:

```bash
# terminal 1
python3 -m uvicorn tests.mock_local_llm:app --port 8080

# terminal 2
langgraph dev
```

Inputs, as entered in the Studio UI:

| Input | Value |
| :--- | :--- |
| `application_name` | `mock_local_llm.py test 1` |
| `llm_provider` | `local` |
| `enable_web_search` | `true` |
| `raw_logs` | contents of `sample_logs/typescript_pino_recovery.log` |

**`error_summary.cascading_impact_summary`** — the batched root-cause pass,
nominating the lowest-volume signature as the mock's heuristic requires:

> ERR_003 is the lowest-volume failure in the batch and is nominated as the
> trigger; the remaining 2 signature(s) are treated as downstream fallout from
> it.

**`pattern_summary.behavioral_synthesis`** — the counts are read back out of the
STATISTICS block the node sent, not invented:

> Mock local model: this narrative restates the reports it was sent and contains
> no reasoning. The window carries 230 error-level and 44 warning-level
> record(s) across 3 named logger(s)...

**`prepare_output.root_cause`** — the signature id, its template, its logger and
its `first_seen` are all echoes of what the error-analysis node published, which
is what makes the synthesis traceable to the batch that produced it:

> Mock local model: ERR_003 (Payment provider unavailable) logged by payment,
> first seen 2026-07-29T10:59:26.622000+00:00, is the signature the error
> analysis nominated, and it is carried through here as the trigger for the
> remaining 2 signature(s) in the batch.

**`prepare_output.executive_summary`** — the same 230 / 44 severity counts the
pattern node reported, now bounded by the timeline's real coverage window:

> Mock local model: this synthesis restates the investigation it was sent, in
> the order the node's own instructions ask for, and contains no reasoning. The
> window runs from 2026-07-29T10:59:21.422000+00:00 to
> 2026-07-29T10:59:56.209000+00:00 and carries 230 error-level and 44
> warning-level record(s)...

Three things are worth reading off that output. The `Mock local model:` prefix
on both synthesis fields confirms `prepare_output` reached the server rather
than its fallback. `ERR_003` appearing in the root cause *and* in the
error-analysis cascade summary confirms the two nodes agree on the same
signature id, which is the merge the mock exists to exercise. And the severity
counts matching across `pattern_summary` and `executive_summary` confirm both
nodes were sent the same statistics report.

---

## Troubleshooting: LangSmith SSL / Timeout Errors on Large Logs

Applies to any traced run, not only the local-mock path above. Tracing is
configured entirely through the environment — `LANGSMITH_TRACING`,
`LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` and `LANGSMITH_ENDPOINT` in `.env`; no
module in `graph_library/` reads or sets them.

**Symptom.** Graph execution succeeds locally — every node completes and the
report renders — but the `langgraph dev` console reports `SSLEOFError`,
`TimeoutError` or `ProtocolError` against
`https://api.smith.langchain.com/runs/multipart` with a `Content-Length` above
10 MB, and no traces appear in the LangSmith dashboard. The run itself is
unaffected: trace upload happens on a background thread, so a failed upload
costs observability rather than results, which is exactly why it is easy to miss.

**Root cause.** `raw_logs` and `parsed_logs` both live in graph state, so the
state snapshot attached to a run span carries the whole payload twice over — once
as text and once as structured entries. Parsing *grows* it: each entry becomes a
`ParsedLogEntry` with its own `metadata` dict, which is roughly 2.3x the raw
bytes it came from. A multi-megabyte corpus file therefore pushes a single
multipart upload past the threshold and the socket times out mid-pass.

Measured, per fixture:

| Fixture | Raw | Entries | `parsed_logs` | State payload |
| :--- | ---: | ---: | ---: | ---: |
| `java_spring_boot_large.json.log` | 3.65 MB | 7,796 | 8.44 MB | **12.09 MB** |
| `typescript_pino_recovery.log` | 0.71 MB | 2,504 | 1.73 MB | 2.44 MB |
| `fastapi_recovery.log` | 0.02 MB | 269 | 0.07 MB | 0.09 MB |

The figures above predate the Prepare Output node, which adds a second copy of
the *analysis* artifacts to the snapshot: `structured_report` carries
`statistics`, `timeline`, `error_summary` and `pattern_summary` verbatim, by
design, so a persisted report is self-contained. It does **not** duplicate
`raw_logs` or `parsed_logs`, which are what the numbers above are dominated by,
so the thresholds and the guidance below still hold — the timeline is the only
one of the four that scales with log volume at all, and it scales with the number
of *buckets*, not entries.

**Solution.**

1. Use a truncated or representative sample for interactive `langgraph dev` runs.
   `sample_logs/fastapi_recovery.log` traces comfortably at 0.09 MB, and
   `typescript_pino_recovery.log` at 2.44 MB still carries a full incident —
   onset, cascade and recovery — which is what the reasoning nodes are being
   exercised on. Log *volume* is not what makes a run interesting; the two
   `java_spring_boot_large.*` fixtures are throughput benchmarks and are the only
   ones that breach the limit.
2. Reserve full-corpus runs for headless performance tests with tracing off:

   ```bash
   LANGSMITH_TRACING=false langgraph dev
   ```

   Use this project's variable, `LANGSMITH_TRACING` — that is what `.env` and
   `.env.example` set. `LANGCHAIN_TRACING_V2` is the legacy alias; the LangSmith
   SDK still honours it, but setting it here leaves `LANGSMITH_TRACING=true` in
   place and tracing stays on.

"""A mock OpenAI-compatible server for exercising the ``local`` provider.

The ``local`` provider targets any server that speaks the OpenAI
chat-completions protocol (vLLM, Ollama, LM Studio). This module is the
smallest such server that satisfies the LLM nodes, so the full path — factory →
``ChatOpenAI`` → HTTP → structured-output parsing → merge — can be exercised
without an API key, a network or a GPU.

**Two nodes reach this server, not one.** ``llm_provider: "local"`` is read from
graph state by every LLM node, so a single Studio run points both the Error
Analysis Node and the Pattern Analysis Node here, and they ask for different
response schemas over the same endpoint:

    * :class:`~graph_library.models.LLMErrorAnalysisResult` — error analysis,
      pass 2 (the root-cause call);
    * :class:`~graph_library.models.LLMSearchDecision` — error analysis, pass 1
      (the optional "is anything here obscure?" triage);
    * :class:`~graph_library.models.PatternAnalysisResult` — pattern analysis.

Answering all three with one shape is not a partial failure but an invisible
one: a schema whose fields all carry defaults validates a foreign payload
without complaint, discards it, and reports an empty analysis. See
:func:`resolve_schema_name` for how a request is routed and
:data:`PAYLOAD_BUILDERS` for what each route answers with.

The response is **derived from the request**, not canned. For error analysis the
handler reads the ``signature_id`` values out of the prompt and returns one
evaluation per signature, nominating the last one listed as the root cause: the
node sends its signatures ranked by descending count, so that is the
lowest-volume signature — the one the system prompt's own heuristic points at.
For pattern analysis it decodes the STATISTICS and TIMELINE reports back out of
the prompt and derives anomalies from the loggers, buckets and metadata
distributions actually in them. Deriving the response makes this a real test of
each node's merge logic (ids must line up, loggers and timestamps must be the
ones that were sent) rather than a fixed blob that would pass even if the node
sent the wrong batch. It performs no reasoning and is not a model.

Nothing here imports the code it stands in front of. The deterministic fallbacks
in ``graph_library.error_analysis.fingerprint`` and
``graph_library.pattern_analysis.fallback`` derive comparable payloads from the
same inputs, and calling them would make the mock agree with the node by
construction — a test could no longer tell "the model answered" from "the model
was unreachable and the fallback ran". The derivations below are deliberately
the mock's own, with their own wording and their own thresholds.

Both transports the protocol defines are served, because the client picks
between them and both are reached in practice:

    * a single JSON ``chat.completion`` body when ``stream`` is false — the
      shape a plain ``.invoke()`` gets;
    * ``text/event-stream`` chunks when ``stream`` is true — the shape
      LangGraph asks for whenever a run subscribes to token streaming, which
      LangGraph Studio does on every graph run.

Serving only the first is *silently* wrong: the client's SSE decoder finds no
events in a JSON body, ends the stream having consumed zero chunks, and the
OpenAI SDK then trips a bare ``assert`` on its own empty snapshot. That reaches
the node as ``AssertionError`` with no message and no hint of where it came
from. See :func:`iter_completion_chunks`.

Nothing in this module raises on a malformed request. An exception inside the
ASGI app propagates out of the transport, through the OpenAI client and into
the node's ``except Exception`` fallback, where it surfaces as an unexplained
"LLM reasoning unavailable" note rather than a test failure that points here.
Missing or unexpected fields are therefore read defensively and answered with a
well-formed response.

Run it standalone::

    python -m uvicorn tests.mock_local_llm:app --port 8000

then point the node at it::

    LOCAL_LLM_BASE_URL=http://127.0.0.1:8000/v1

Or use it in-process, without a socket, via :func:`make_transport` — see
``tests/test_error_analysis.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from typing import Any

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="LogSherlock mock local LLM")

#: Pulls the signature ids out of the JSON batch the node renders into the
#: prompt. Deliberately a regex over the raw text: the mock stands in for a
#: model, which likewise only ever sees a string.
_SIGNATURE_ID_PATTERN = re.compile(r'"signature_id":\s*"([^"]+)"')

#: The section headings the pattern-analysis prompt is built from. Each one is
#: followed by either a JSON block or a parenthesised "(unavailable ...)" line,
#: and matching them all in one pass is what bounds each block: a section's
#: payload ends where the next heading begins. See
#: :func:`extract_pattern_inputs`.
_SECTION_HEADER_PATTERN = re.compile(
    r"^(STATISTICS|TIMELINE|INVESTIGATION NOTES)\b.*$", re.MULTILINE
)

#: Decodes one JSON value out of the middle of a larger string. ``raw_decode``
#: stops at the end of the value rather than demanding that the whole string be
#: JSON, which is what makes it usable on a prompt.
_DECODER = json.JSONDecoder()

#: Model id echoed back in the response envelope.
MODEL_NAME = "mock-local-llm"

#: How many pieces a streamed payload is cut into. More than one on purpose:
#: a single-chunk stream would not exercise the client's accumulator, which is
#: the half of the streaming path most likely to break.
STREAM_CHUNK_COUNT = 4

#: The schema answered by a request that names none — every historical caller
#: of this module, and any client using a method that puts neither a tool nor a
#: named response format on the wire.
DEFAULT_SCHEMA = "LLMErrorAnalysisResult"

#: How many metadata dimensions the pattern payload reports on. The node's
#: prompt sends every low-cardinality key it discovered, which for a rich JSON
#: log is a dozen or more; a mock that echoed all of them back would bury the
#: one that matters.
MAX_METADATA_INSIGHTS = 5

#: How many loggers a derived cascade names. The bound exists for the same
#: reason: a sentence naming twenty components is not a finding.
MAX_NAMED_LOGGERS = 3


def prompt_text(messages: list[dict[str, Any]]) -> str:
    """Flatten a chat-completions message list into the text a model would see.

    Args:
        messages: The ``messages`` array from the request body.

    Returns:
        Every string turn joined by newlines, in order. Content is a list for
        multimodal turns and absent for tool results; neither carries a prompt,
        and neither may crash the handler.
    """
    if not isinstance(messages, list):
        return ""
    return "\n".join(
        message["content"]
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("content"), str)
    )


def extract_signature_ids(messages: list[dict[str, Any]]) -> list[str]:
    """Read the signature ids out of a chat-completions message list.

    Args:
        messages: The ``messages`` array from the request body.

    Returns:
        The ids in prompt order, de-duplicated. Empty when the prompt carries
        none, which the caller renders as a "no signatures" response.
    """
    seen: dict[str, None] = {}
    for signature_id in _SIGNATURE_ID_PATTERN.findall(prompt_text(messages)):
        seen.setdefault(signature_id, None)
    return list(seen)


def build_analysis_payload(signature_ids: list[str]) -> dict[str, Any]:
    """Build a response body matching :class:`~graph_library.models.LLMErrorAnalysisResult`.

    Every id gets an evaluation, so the node's "did not evaluate" note stays
    silent and a genuine coverage gap in the node would show up. The last id —
    the lowest-volume signature, since the node ranks by descending count — is
    nominated as the root cause; the rest are marked as downstream fallout.

    Args:
        signature_ids: The ids read out of the prompt, in prompt order.

    Returns:
        A plain dict with the schema's three fields. Keys and types match
        exactly; the caller serializes it. With no ids the root cause is
        ``None``, which is the schema's way of saying "cannot single one out".
    """
    if not signature_ids:
        return {
            "primary_error_signature_id": None,
            "cascading_impact_summary": (
                "No error signatures were supplied, so there is no cascade to "
                "describe."
            ),
            "evaluations": [],
        }

    primary = signature_ids[-1]
    secondary_count = len(signature_ids) - 1

    return {
        "primary_error_signature_id": primary,
        "cascading_impact_summary": (
            f"{primary} is the lowest-volume failure in the batch and is "
            f"nominated as the trigger; the remaining {secondary_count} "
            f"signature(s) are treated as downstream fallout from it."
        ),
        "evaluations": [
            {
                "signature_id": signature_id,
                "is_root_cause_candidate": signature_id == primary,
                "explanation": (
                    f"{signature_id} is nominated as the root cause: it is the "
                    "lowest-volume signature in the batch."
                    if signature_id == primary
                    else f"{signature_id} is a downstream consequence of {primary}."
                ),
            }
            for signature_id in signature_ids
        ],
    }


def build_search_decision_payload(signature_ids: list[str]) -> dict[str, Any]:
    """Build a response body matching :class:`~graph_library.models.LLMSearchDecision`.

    The mock always declines the lookup, which is the answer the real system
    prompt calls "the normal answer" — most payloads are ordinary connection
    refusals and null dereferences that need no documentation. Declining is also
    the only answer that keeps a local run local: a query here is not answered
    by this server but by the Web Search Node, which calls Tavily over the real
    network with a real credential. A mock that manufactured queries would turn
    "point the graph at my laptop" into billed third-party traffic.

    That makes the empty list deliberate rather than accidental. Until this
    module routed by schema it answered the decision pass with an error-analysis
    payload, which ``LLMSearchDecision`` — every field defaulted — accepted in
    silence and read as "no search wanted". The observable behaviour is
    unchanged; what changed is that it is now a decision with a stated reason
    instead of a dropped payload.

    Args:
        signature_ids: The ids read out of the prompt, in prompt order.

    Returns:
        A plain dict with the schema's two fields.
    """
    return {
        "queries": [],
        # Surfaced by the node's own log line, so an operator watching a local
        # run sees why the search loop never fires.
        "reasoning": (
            f"Triaged {len(signature_ids)} signature(s); the mock local model "
            "never requests a lookup, so that a local run stays offline."
        ),
    }


def _decode_json_block(text: str, start: int, stop: int) -> Any:
    """Decode the JSON value opening between ``start`` and ``stop``, if any.

    Args:
        text: The prompt text.
        start: Where to begin looking — the end of a section heading.
        stop: Where to stop looking — the start of the next heading. Bounding
            the search is what stops an absent section from picking up the next
            section's payload: ``"STATISTICS\\n(unavailable — ...)"`` carries no
            bracket, and an unbounded scan would happily return the timeline's.

    Returns:
        The decoded value, or ``None`` when the span holds no JSON. The decode
        itself runs unbounded, since a value that starts inside the span ends
        where its own syntax ends.
    """
    openings = [
        index
        for index in (text.find("[", start, stop), text.find("{", start, stop))
        if index != -1
    ]
    if not openings:
        return None
    try:
        value, _end = _DECODER.raw_decode(text, min(openings))
    except ValueError:
        return None
    return value


def extract_pattern_inputs(
    messages: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Recover the statistics and timeline reports out of a pattern prompt.

    The Pattern Analysis Node renders both of its inputs into the prompt as
    ``json.dumps`` blocks under fixed headings, so they can be read back exactly
    — real logger names, real timestamps, real metadata values. Deriving the
    answer from those is what makes the mock's output checkable against the
    input the node actually sent.

    Args:
        messages: The ``messages`` array from the request body.

    Returns:
        A ``(statistics, timeline)`` pair. Either may be empty: the prompt
        states an absent report as prose rather than as an empty JSON value, and
        a request that is not a pattern prompt at all yields both empty. The
        timeline concatenates the milestone and bucket blocks, in that order,
        which is the order the prompt renders them in.
    """
    text = prompt_text(messages)
    statistics: dict[str, Any] = {}
    timeline: list[dict[str, Any]] = []

    headers = list(_SECTION_HEADER_PATTERN.finditer(text))
    for index, header in enumerate(headers):
        stop = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        block = _decode_json_block(text, header.end(), stop)

        if header.group(1) == "STATISTICS" and isinstance(block, dict):
            statistics = block
        elif header.group(1) == "TIMELINE" and isinstance(block, list):
            timeline.extend(event for event in block if isinstance(event, dict))

    return statistics, timeline


def _int(value: Any) -> int:
    """Read a count out of a decoded payload without trusting its type."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _severity_for(statistics: dict[str, Any]) -> str:
    """Pick an anomaly severity from the dataset-wide error share.

    The thresholds are the mock's own — see the module docstring on why nothing
    here is imported from the node it answers.
    """
    severity = statistics.get("severity")
    if not isinstance(severity, dict):
        return "info"
    try:
        ratio = float(severity.get("error_ratio") or 0.0)
    except (TypeError, ValueError):
        return "info"
    if ratio >= 0.25:
        return "critical"
    if ratio >= 0.05:
        return "warning"
    return "info"


def _named_loggers(statistics: dict[str, Any]) -> list[tuple[str, int]]:
    """The logger distribution as ``(name, count)``, most frequent first.

    Records with no logger appear in the distribution under a ``None`` value and
    are dropped here: "unnamed" is not a component, and naming it in a cascade
    would put a value in ``affected_loggers`` that appears nowhere in the logs.
    """
    rows = statistics.get("logger_distribution")
    if not isinstance(rows, list):
        return []
    return [
        (str(row.get("value")), _int(row.get("count")))
        for row in rows
        if isinstance(row, dict) and row.get("value") is not None
    ]


def _metadata_concentrations(
    statistics: dict[str, Any],
) -> list[tuple[float, str, Any, int, int]]:
    """Rank the metadata dimensions by how concentrated their top value is.

    Returns:
        ``(share, key, value, count, total)`` tuples, strongest share first with
        the key breaking ties so the order is stable. Capped at
        :data:`MAX_METADATA_INSIGHTS`. Single-valued keys are kept — unlike the
        node's own fallback, which discards them as constants — because the mock
        is demonstrating that it read the distribution, not making a finding.
    """
    distributions = statistics.get("metadata_distributions")
    if not isinstance(distributions, dict):
        return []

    found: list[tuple[float, str, Any, int, int]] = []
    for key, rows in distributions.items():
        if not isinstance(rows, list) or not rows:
            continue
        counted = [row for row in rows if isinstance(row, dict)]
        total = sum(_int(row.get("count")) for row in counted)
        if total <= 0:
            continue
        top = max(counted, key=lambda row: _int(row.get("count")))
        count = _int(top.get("count"))
        found.append((count / total, str(key), top.get("value"), count, total))

    found.sort(key=lambda item: (-item[0], item[1]))
    return found[:MAX_METADATA_INSIGHTS]


def _pattern_anomalies(
    statistics: dict[str, Any], timeline: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Derive the anomaly list from the two decoded reports.

    One rule per :data:`~graph_library.models.AnomalyCategory` value, each
    reporting a number that is present in the input, so an anomaly the mock
    emits can always be traced back to the prompt that produced it. Every rule
    is skippable: an empty list is a valid ``PatternAnalysisResult``, and a
    payload with no timeline and no metadata should produce one.
    """
    severity = _severity_for(statistics)
    loggers = _named_loggers(statistics)
    buckets = [event for event in timeline if event.get("event_type") == "bucket"]
    anomalies: list[dict[str, Any]] = []

    if buckets:
        counts = [_int(bucket.get("error_count")) for bucket in buckets]
        mean = sum(counts) / len(counts)
        # ``max`` keeps the first maximal bucket, so a flat series resolves to
        # its opening window and the choice stays deterministic.
        peak = max(
            buckets,
            key=lambda bucket: (
                _int(bucket.get("error_count")),
                _int(bucket.get("total_logs")),
            ),
        )
        anomalies.append(
            {
                "category": "volume_spike",
                "severity": severity,
                "description": (
                    f"Volume peaks in the bucket starting {peak.get('timestamp')}: "
                    f"{_int(peak.get('total_logs'))} record(s), "
                    f"{_int(peak.get('error_count'))} of them errors, against a "
                    f"series mean of {mean:.1f} error(s) across {len(buckets)} "
                    "bucket(s)."
                ),
                "affected_loggers": [
                    str(name) for name in (peak.get("top_loggers") or [])
                ],
                "time_window": peak.get("timestamp"),
            }
        )

        if len(buckets) >= 2 and counts[0] != counts[-1]:
            direction = "above" if counts[-1] > counts[0] else "below"
            anomalies.append(
                {
                    "category": "baseline_shift",
                    "severity": severity,
                    "description": (
                        f"The series ends {direction} where it started: "
                        f"{counts[0]} error(s) in the first bucket against "
                        f"{counts[-1]} in the last, over {len(buckets)} bucket(s)."
                    ),
                    "affected_loggers": [
                        str(name) for name in (buckets[-1].get("top_loggers") or [])
                    ],
                    "time_window": buckets[-1].get("timestamp"),
                }
            )

    if len(loggers) >= 2:
        named = loggers[:MAX_NAMED_LOGGERS]
        anomalies.append(
            {
                "category": "logger_cascade",
                "severity": severity,
                "description": (
                    f"{len(loggers)} components logged during the window, led by "
                    + ", ".join(f"{name} ({count})" for name, count in named)
                    + ". The ranking is by volume and does not establish an order."
                ),
                "affected_loggers": [name for name, _count in named],
                "time_window": None,
            }
        )

    concentrations = _metadata_concentrations(statistics)
    if concentrations:
        share, key, value, count, total = concentrations[0]
        anomalies.append(
            {
                "category": "metadata_clustering",
                "severity": "info",
                "description": (
                    f"Activity concentrates on one value of {key!r}: {value!r} "
                    f"covers {count} of {total} record(s) ({share:.0%}), the "
                    "strongest concentration of the "
                    f"{len(concentrations)} dimension(s) reported."
                ),
                "affected_loggers": [],
                "time_window": None,
            }
        )

    return anomalies


def _pattern_synthesis(
    statistics: dict[str, Any],
    timeline: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
) -> str:
    """The narrative for ``behavioral_synthesis``, which is never empty.

    The field is the one on the schema with no default, so a blank here is a
    validation error rather than a thin answer. It is written even for an input
    the mock could recover nothing from — stating that is more useful to whoever
    is reading a local run than an empty string.
    """
    severity = statistics.get("severity")
    severity = severity if isinstance(severity, dict) else {}
    coverage = statistics.get("timestamp_coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    buckets = [event for event in timeline if event.get("event_type") == "bucket"]
    milestones = [event for event in timeline if event.get("event_type") == "milestone"]

    parts = [
        "Mock local model: this narrative restates the reports it was sent and "
        "contains no reasoning.",
        f"The window carries {_int(severity.get('error_count'))} error-level and "
        f"{_int(severity.get('warning_count'))} warning-level record(s) across "
        f"{len(_named_loggers(statistics))} named logger(s).",
    ]

    if coverage.get("earliest") and coverage.get("latest"):
        parts.append(
            f"Coverage runs from {coverage['earliest']} to {coverage['latest']}, "
            f"placed into {len(buckets)} bucket(s) with {len(milestones)} "
            "milestone(s)."
        )
    else:
        parts.append(
            f"The prompt carried {len(buckets)} bucket(s) and "
            f"{len(milestones)} milestone(s)."
        )

    parts.append(
        f"{len(anomalies)} anomaly/anomalies were derived from those numbers."
        if anomalies
        else "Nothing in the reports met the mock's anomaly rules, so none were "
        "reported."
    )
    return " ".join(parts)


def build_pattern_payload(
    statistics: dict[str, Any], timeline: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build a response body matching :class:`~graph_library.models.PatternAnalysisResult`.

    Args:
        statistics: The statistics report decoded back out of the prompt.
        timeline: The timeline events decoded back out of the prompt.

    Returns:
        A plain dict with the schema's four fields. Keys, types and the closed
        ``category`` / ``severity`` vocabularies match exactly; the caller
        serializes it. Empty inputs give empty lists and a synthesis that says
        so, which is a valid answer rather than a degraded one.
    """
    anomalies = _pattern_anomalies(statistics, timeline)
    loggers = _named_loggers(statistics)
    concentrations = _metadata_concentrations(statistics)

    correlations: list[str] = []
    if len(loggers) >= 2:
        first, second = loggers[0], loggers[1]
        correlations.append(
            f"{first[0]} and {second[0]} are the two busiest components "
            f"({first[1]} and {second[1]} record(s)); they share the window but "
            "no ordering between them was established."
        )

    return {
        "anomalies": anomalies,
        "cross_logger_correlations": correlations,
        "metadata_insights": [
            f"{key}={value!r} covers {share:.0%} of records ({count}/{total})."
            for share, key, value, count, total in concentrations
        ],
        "behavioral_synthesis": _pattern_synthesis(statistics, timeline, anomalies),
    }


def resolve_tool_name(body: dict[str, Any]) -> str | None:
    """Return the tool the request asked to be called, if it sent any.

    The name is echoed from the request rather than hardcoded: the client
    matches the returned call by name against the schema it sent, so a guess
    that drifts from the schema's title silently drops the call.

    Args:
        body: The decoded chat-completions request.

    Returns:
        The first tool's function name, or ``None`` when the request carries no
        ``tools`` — which is the case for the ``json_schema`` path. An unnamed
        tool falls back to the schema the payload was built for, so the call the
        client is offered and the arguments inside it always agree.
    """
    tools = body.get("tools") or []
    if not isinstance(tools, list) or not tools or not isinstance(tools[0], dict):
        return None
    function = tools[0].get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return name if isinstance(name, str) and name else resolve_schema_name(body)


def _requested_schema(body: dict[str, Any]) -> dict[str, Any] | None:
    """Return the JSON Schema the request bound, from wherever it put it.

    ``with_structured_output`` sends the schema in one of two places depending
    on the method the client chose, and this module has to read both: under
    ``response_format.json_schema`` for ``json_schema`` (what the ``local``
    provider defaults to) and under ``tools[0].function`` for
    ``function_calling`` (what DeepSeek is pinned to, and what a differently
    configured local endpoint may use).
    """
    response_format = body.get("response_format")
    if isinstance(response_format, dict):
        json_schema = response_format.get("json_schema")
        if isinstance(json_schema, dict):
            return json_schema

    tools = body.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[0], dict):
        function = tools[0].get("function")
        if isinstance(function, dict):
            return function

    return None


def resolve_schema_name(body: dict[str, Any]) -> str:
    """Decide which response schema this request is asking to be filled in.

    Three signals, tried in descending order of directness, because the first
    is not always present and answering the wrong schema is a silent failure
    rather than a loud one:

        1. **The declared name.** Both structured-output methods carry the
           schema's class name on the wire — ``response_format.json_schema.name``
           or ``tools[0].function.name``. This is the signal in practice.
        2. **The declared fields.** A schema bound under a different name is
           still recognizable by its own top-level properties, and
           :data:`SCHEMA_MARKER_FIELDS` names one that only it has.
        3. **The prompt.** ``method="json_mode"`` puts neither a name nor a
           schema on the wire, leaving only what the node wrote — the same
           position a real model is in. :data:`PROMPT_MARKERS` holds one
           distinguishing phrase per prompt.

    Args:
        body: The decoded chat-completions request.

    Returns:
        A key of :data:`PAYLOAD_BUILDERS`, falling back to
        :data:`DEFAULT_SCHEMA` when nothing in the request identifies one.
    """
    schema = _requested_schema(body) or {}

    name = schema.get("name")
    if isinstance(name, str) and name in PAYLOAD_BUILDERS:
        return name

    # ``json_schema`` nests the schema under ``schema``; ``function_calling``
    # under ``parameters``. Only the properties are read, so the two are
    # interchangeable here.
    definition = schema.get("schema") or schema.get("parameters")
    if isinstance(definition, dict):
        properties = definition.get("properties")
        if isinstance(properties, dict):
            for field, schema_name in SCHEMA_MARKER_FIELDS:
                if field in properties:
                    return schema_name

    text = prompt_text(body.get("messages") or [])
    for marker, schema_name in PROMPT_MARKERS:
        if marker in text:
            return schema_name

    return DEFAULT_SCHEMA


def _error_analysis_response(body: dict[str, Any]) -> dict[str, Any]:
    """Answer the root-cause pass from the signature ids in the prompt."""
    return build_analysis_payload(extract_signature_ids(body.get("messages") or []))


def _search_decision_response(body: dict[str, Any]) -> dict[str, Any]:
    """Answer the search-triage pass from the signature ids in the prompt."""
    return build_search_decision_payload(
        extract_signature_ids(body.get("messages") or [])
    )


def _pattern_analysis_response(body: dict[str, Any]) -> dict[str, Any]:
    """Answer the pattern pass from the two reports embedded in the prompt."""
    return build_pattern_payload(*extract_pattern_inputs(body.get("messages") or []))


#: One builder per schema this server answers, keyed by the name the client puts
#: on the wire. Adding an LLM node means adding a row here — the alternative is
#: the node receiving another node's payload, which its schema may well accept.
PAYLOAD_BUILDERS: dict[str, Any] = {
    "LLMErrorAnalysisResult": _error_analysis_response,
    "LLMSearchDecision": _search_decision_response,
    "PatternAnalysisResult": _pattern_analysis_response,
}

#: A top-level field that belongs to exactly one of the schemas above, used to
#: route a request whose schema was bound under some other name. Ordered, and
#: checked in order, so a future overlap resolves predictably rather than by
#: dict iteration.
SCHEMA_MARKER_FIELDS: tuple[tuple[str, str], ...] = (
    ("evaluations", "LLMErrorAnalysisResult"),
    ("anomalies", "PatternAnalysisResult"),
    ("queries", "LLMSearchDecision"),
)

#: A phrase that appears in exactly one of the three prompts, for the last-ditch
#: case where the request declares no schema at all. Deliberately short and
#: structural — a heading and a closing instruction — rather than a long
#: quotation that would rot the first time a prompt is reworded.
PROMPT_MARKERS: tuple[tuple[str, str], ...] = (
    ("\nSTATISTICS\n", "PatternAnalysisResult"),
    ("return the queries you would run", "LLMSearchDecision"),
)


def build_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Build the response payload for whichever schema this request bound.

    Args:
        body: The decoded chat-completions request.

    Returns:
        A plain dict matching the resolved schema exactly.
    """
    return PAYLOAD_BUILDERS[resolve_schema_name(body)](body)


def _serialized_payload(body: dict[str, Any]) -> str:
    """Render this request's payload as a JSON string.

    A *string*, in both the content and the tool-arguments position: the wire
    format for tool arguments is text, and a raw object there fails the
    client's parse.
    """
    return json.dumps(build_payload(body))


def _split_evenly(text: str, parts: int) -> list[str]:
    """Cut ``text`` into ``parts`` non-empty pieces, as a token stream would."""
    if parts < 2 or len(text) < parts:
        return [text]
    size = -(-len(text) // parts)  # ceiling, so the pieces cover the whole text
    return [text[start : start + size] for start in range(0, len(text), size)]


def build_completion_response(body: dict[str, Any]) -> dict[str, Any]:
    """Wrap this request's payload in a non-streaming chat-completion envelope.

    Both structured-output methods are served from the same payload, because
    which one is used is the client's choice, not the server's:
    ``with_structured_output`` currently sends ``response_format:
    {"type": "json_schema"}`` and reads ``message.content``, but a client
    configured with ``method="function_calling"`` sends ``tools`` and reads
    ``message.tool_calls`` instead.

    Args:
        body: The decoded chat-completions request.

    Returns:
        The response envelope, ready to be serialized.
    """
    serialized = _serialized_payload(body)
    tool_name = resolve_tool_name(body)

    if tool_name is not None:
        message: dict[str, Any] = {
            "role": "assistant",
            # Null alongside a tool call, as a real provider sends it.
            "content": None,
            "tool_calls": [
                {
                    "id": "call_mock_0",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": serialized},
                }
            ],
        }
    else:
        message = {"role": "assistant", "content": serialized}

    return {
        "id": "chatcmpl-mock-123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": body.get("model") or MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_name is not None else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        },
    }


def build_completion_chunks(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the ``chat.completion.chunk`` sequence for a streaming request.

    The same payload as :func:`build_completion_response`, delivered the way a
    streaming server delivers it: an opening chunk that declares the role, one
    chunk per slice of the payload, and a terminal chunk carrying the
    ``finish_reason`` and no delta. Clients accumulate these into a single
    message, so the concatenated slices must reproduce the payload exactly.

    Args:
        body: The decoded chat-completions request.

    Returns:
        The chunks in wire order, excluding the ``[DONE]`` sentinel.
    """
    serialized = _serialized_payload(body)
    tool_name = resolve_tool_name(body)
    pieces = _split_evenly(serialized, STREAM_CHUNK_COUNT)

    def envelope(delta: dict[str, Any], finish_reason: str | None) -> dict[str, Any]:
        return {
            "id": "chatcmpl-mock-123",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": body.get("model") or MODEL_NAME,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    if tool_name is not None:
        # The id, type and name arrive once, on the opening tool-call delta;
        # every later delta carries only more argument text, addressed by the
        # same ``index``. Repeating the name would append it to itself.
        chunks = [
            envelope(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_mock_0",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": ""},
                        }
                    ],
                },
                None,
            )
        ]
        chunks += [
            envelope(
                {"tool_calls": [{"index": 0, "function": {"arguments": piece}}]}, None
            )
            for piece in pieces
        ]
        chunks.append(envelope({}, "tool_calls"))
    else:
        chunks = [envelope({"role": "assistant", "content": ""}, None)]
        chunks += [envelope({"content": piece}, None) for piece in pieces]
        chunks.append(envelope({}, "stop"))

    # Only sent when asked for; an unsolicited usage chunk is a protocol
    # violation that some clients reject.
    stream_options = body.get("stream_options")
    if isinstance(stream_options, dict) and stream_options.get("include_usage"):
        usage_chunk = envelope({}, None)
        usage_chunk["choices"] = []
        usage_chunk["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        chunks.append(usage_chunk)

    return chunks


def iter_completion_chunks(body: dict[str, Any]) -> Iterator[str]:
    """Render the streaming response as server-sent events.

    Each event is ``data: <json>`` followed by a blank line, and the stream is
    closed by the ``[DONE]`` sentinel. Getting this framing wrong does not
    produce an error the client can report: its SSE decoder simply finds no
    events, the stream ends having yielded nothing, and the OpenAI SDK asserts
    on its own un-initialized snapshot — surfacing as a bare ``AssertionError``
    with no message, four libraries away from the cause.

    Args:
        body: The decoded chat-completions request.

    Yields:
        The encoded events, in wire order.
    """
    for chunk in build_completion_chunks(body):
        yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(body: dict[str, Any]) -> Response:
    """The one endpoint the ``local`` provider needs.

    Honours ``stream``. A client that asked for events and got a JSON body
    fails in a way that names neither this server nor the request that caused
    it, so the branch is not optional.
    """
    if body.get("stream"):
        return StreamingResponse(
            iter_completion_chunks(body),
            media_type="text/event-stream",
        )
    return JSONResponse(build_completion_response(body))


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    """Advertise the served model; some clients probe this on startup."""
    return JSONResponse(
        {
            "object": "list",
            "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "logsherlock"}],
        }
    )


class SyncASGITransport(httpx.BaseTransport):
    """Serve an ASGI app to a *synchronous* httpx client, in-process.

    ``httpx.ASGITransport`` only implements the async half of the transport
    protocol, but the OpenAI SDK that ``ChatOpenAI`` wraps drives a sync
    ``httpx.Client``. This adapter runs each request through the ASGI app on a
    private event loop and hands the result back synchronously, which is what
    lets a test exercise the real client — real request building, real response
    parsing — with no socket and nothing to tear down.
    """

    def __init__(self, asgi_app: Any) -> None:
        self._app = asgi_app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()

        async def call() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://mock"
            ) as client:
                return await client.request(
                    request.method,
                    request.url,
                    content=body,
                    headers={
                        key: value
                        for key, value in request.headers.items()
                        # Dropped because httpx recomputes them for the inner
                        # request; forwarding the outer values would conflict.
                        if key.lower() not in {"content-length", "host"}
                    },
                )

        response = asyncio.run(call())
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"content-type": response.headers.get("content-type", "application/json")},
            request=request,
        )


def make_transport() -> httpx.BaseTransport:
    """Return a sync httpx transport that serves this app in-process.

    Pass the resulting client to the factory::

        import httpx
        client = httpx.Client(transport=make_transport(), base_url="http://mock")
        llm = get_error_analysis_llm("local", "fast", http_client=client)
    """
    return SyncASGITransport(app)


if __name__ == "__main__":  # pragma: no cover - manual entry point
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

"""Prompt construction for the Prepare Output Node.

This node is the only one that sees every upstream artifact at once, so its
prompt is an assembly problem rather than a serialization one: five payloads
built for five different readers have to arrive in one turn, labelled clearly
enough that the model can align a logger name in the error signatures with the
same name in the timeline.

Three decisions shape what gets sent:

    * **The statistics, timeline and investigation-note sections are the ones
      the pattern-analysis node already renders.** They are imported from
      :mod:`graph_library.pattern_analysis.prompts` rather than reimplemented —
      that module's formatters are pure functions over the shared models, they
      are part of its public surface, and the trimming policy they encode
      (milestones never dropped, busiest buckets kept, omissions stated) is
      exactly the policy this node wants. Reimplementing them would fork a
      hundred lines of JSON layout that must agree in order for two nodes to be
      comparable when their answers disagree.
    * **Provenance is stated section by section.** The error and pattern
      summaries are themselves model output, and a synthesis that treats another
      model's inference as a measured fact compounds the first model's error.
      The section headings and the system prompt both say which is which.
    * **Parser health is included, and the confidence score is not.** The
      metrics are what the executive summary's data-quality caveat has to be
      built from. The deterministic score computed from those same metrics is
      deliberately withheld: the model is asked to rate the clarity of the
      signal it was shown, and handing it the answer would collapse the two
      independent readings into one.
"""

from __future__ import annotations

import json
from typing import Any

from graph_library.models import (
    ErrorSummary,
    ParserMetrics,
    PatternSummary,
    Statistics,
    TimelineEvent,
)

# Reused rather than reimplemented — see the module docstring. These are
# imported from the pattern-analysis package's public surface; nothing in that
# package is modified by this one.
from graph_library.pattern_analysis.prompts import (
    format_investigation_notes,
    format_statistics,
    format_timeline,
)

#: The node's standing instructions. Written against the three failure modes a
#: model shows when handed a complete investigation: restating the inputs
#: instead of concluding from them, promoting the loudest error to the cause,
#: and writing with uniform certainty regardless of how much of the payload was
#: actually readable.
SYSTEM_PROMPT = """\
You are the incident commander writing up a completed log investigation.

Four analysis stages have already run over one application's logs, and you are \
given all of their output:
- PARSER HEALTH: how much of the payload was actually readable. Deterministic.
- STATISTICS: what the dataset contains — level and logger distributions, \
severity counts, timestamp coverage, metadata distributions. Deterministic.
- TIMELINE: how the window unfolded — fixed-width buckets and the milestones \
marking first error, onset, peak and recovery. Deterministic.
- ERROR ANALYSIS: error signatures with counts, plus another model's nomination \
of a primary signature and its account of the cascade. Partly inferred.
- PATTERN ANALYSIS: another model's reading of the behavioral patterns across \
the statistics and the timeline. Inferred.
- INVESTIGATION NOTES: what the deterministic passes could not measure.
- HISTORICAL CONTEXT: summaries of previous investigations, when the caller \
supplied any.

Your task is to produce the investigation's conclusion: a one-sentence root \
cause, a multi-paragraph executive summary, and your confidence in the \
diagnosis.

Rules:
- The ROOT CAUSE is one sentence naming the core trigger — the failure that \
started the incident, not the symptom that produced the most log lines. The \
loudest signature is frequently downstream noise.
- The EXECUTIVE SUMMARY is several paragraphs and must cover, in this order: \
what happened and when (use the timeline milestones), how the failure spread \
(use the error cascade and the cross-logger patterns), and what limits this \
conclusion (use the parser health and the investigation notes).
- State the data-quality caveat explicitly whenever lines were unreadable or \
entries carried no timestamp. A conclusion drawn from a payload that was two \
thirds parseable must say so in the summary rather than only in a score.
- Ground every claim in the input. Do not invent loggers, timestamps, counts, \
error templates or metadata values.
- Distinguish measurement from inference. The statistics, the timeline and the \
signature counts are facts; the primary-signature nomination and the behavioral \
patterns are another model's conclusions, and you may disagree with them — say \
so, and say why, rather than restating them.
- When the evidence does not single out a cause, say that plainly and describe \
the leading candidates. An honest "the payload does not distinguish between a \
connection pool exhaustion and an upstream timeout" is worth more than a \
confident guess.
- Compare against HISTORICAL CONTEXT when it is present: a recurrence, a \
regression or a drift from previous investigations belongs in the summary.
- LLM_CONFIDENCE_SCORE rates the clarity of the signal you were shown — how \
unambiguously this evidence points at one cause. Do not discount it for \
missing or malformed data; that is scored separately and deterministically.\
"""

#: Error-signature fields sent to the model, in render order.
#: ``sample_messages`` is excluded: the error-analysis node has already read
#: those raw messages and published an ``explanation`` per signature, so
#: resending them costs the largest share of this prompt's tokens to repeat
#: evidence that has been summarized once already.
ERROR_SIGNATURE_FIELDS: tuple[str, ...] = (
    "signature_id",
    "template",
    "severity",
    "count",
    "first_seen",
    "last_seen",
    "loggers",
    "is_root_cause_candidate",
    "explanation",
)

#: Parser-metric fields sent to the model. The whole payload — every field is
#: evidence for the data-quality caveat the system prompt requires.
PARSER_METRIC_FIELDS: tuple[str, ...] = (
    "detected_format",
    "parser_name",
    "parser_confidence",
    "total_lines",
    "blank_lines",
    "parsed_lines",
    "malformed_lines",
    "missing_timestamp_lines",
)

#: How many error signatures may be sent. The error-analysis node caps its own
#: batch at 25 and orders them by descending count, so this keeps the loudest
#: dozen — comfortably more than any cascade narrative uses — and states the
#: omission rather than hiding it.
MAX_ERROR_SIGNATURES = 12

#: How many previous investigations may be sent, most recent first. Historical
#: context is the least bounded input here (the caller controls its shape and
#: size entirely) and the least load-bearing: it qualifies a conclusion as a
#: recurrence, it does not establish one.
MAX_HISTORICAL_INVESTIGATIONS = 3

#: Characters per rendered historical investigation. A caller passing whole
#: stored reports rather than summaries would otherwise dominate the prompt.
MAX_HISTORICAL_CHARS = 2000


def format_parser_health(parser_metrics: ParserMetrics | None) -> str:
    """Render ingestion health as the PARSER HEALTH section.

    Args:
        parser_metrics: The parser's metrics. ``None`` and ``{}`` render as an
            explicit statement that the section is empty rather than as an
            empty object — "we do not know how much was readable" is itself a
            caveat the summary should carry.

    Returns:
        The rendered section, without a trailing newline.
    """
    if not parser_metrics:
        return (
            "PARSER HEALTH\n(unavailable — no parser metrics were reported, so "
            "how much of the payload was readable is unknown)"
        )

    payload = {
        field: parser_metrics[field]  # type: ignore[literal-required]
        for field in PARSER_METRIC_FIELDS
        if field in parser_metrics
    }
    return f"PARSER HEALTH\n{json.dumps(payload, indent=2, default=str)}"


def format_error_summary(error_summary: ErrorSummary | None) -> str:
    """Render the error analysis as the ERROR ANALYSIS section.

    The summary-level fields are rendered ahead of the signatures and labelled
    as another model's conclusions, because they are the two values in this
    prompt most likely to be adopted verbatim: a synthesis that simply repeats
    ``primary_error_signature_id`` has added nothing and has laundered an
    inference into a finding.

    Args:
        error_summary: The Error Analysis Node's output. ``None`` and ``{}``
            render as an explicit empty section.

    Returns:
        The rendered section, without a trailing newline.
    """
    if not error_summary:
        return (
            "ERROR ANALYSIS\n(unavailable — the error-analysis node produced "
            "nothing)"
        )

    signatures = error_summary.get("signatures") or []
    kept = signatures[:MAX_ERROR_SIGNATURES]
    payload = [
        {
            field: signature[field]  # type: ignore[literal-required]
            for field in ERROR_SIGNATURE_FIELDS
            if field in signature
        }
        for signature in kept
    ]

    header = {
        "total_errors_analyzed": error_summary.get("total_errors_analyzed"),
        "unique_signatures_found": error_summary.get("unique_signatures_found"),
        "primary_error_signature_id": error_summary.get("primary_error_signature_id"),
        "cascading_impact_summary": error_summary.get("cascading_impact_summary"),
    }

    sections = [
        "ERROR ANALYSIS — summary (the nomination and the cascade account are "
        "another model's conclusions)",
        json.dumps(header, indent=2, default=str),
        f"ERROR ANALYSIS — {len(kept)} signature(s), by descending count "
        "(counts and templates are deterministic)",
    ]

    if len(signatures) > len(kept):
        # Stated rather than silent: a model told it has every signature will
        # read the absence of one as evidence it never occurred.
        sections.append(
            f"(the {len(signatures) - len(kept)} lowest-volume signature(s) "
            "were omitted to fit)"
        )

    sections.append(json.dumps(payload, indent=2, default=str))
    return "\n".join(sections)


def format_pattern_summary(pattern_summary: PatternSummary | None) -> str:
    """Render the behavioral patterns as the PATTERN ANALYSIS section.

    Sent whole. Unlike the timeline it is bounded by construction — a handful of
    anomalies, correlations and insights — so there is nothing to trim.

    Args:
        pattern_summary: The Pattern Analysis Node's output. ``None`` and ``{}``
            render as an explicit empty section.

    Returns:
        The rendered section, without a trailing newline.
    """
    if not pattern_summary:
        return (
            "PATTERN ANALYSIS\n(unavailable — the pattern-analysis node "
            "produced nothing)"
        )

    return (
        "PATTERN ANALYSIS (another model's conclusions, drawn from the "
        "statistics and timeline below)\n"
        f"{json.dumps(pattern_summary, indent=2, default=str)}"
    )


def format_historical_context(
    historical_context: list[dict[str, Any]] | None,
) -> str:
    """Render previous investigations as the HISTORICAL CONTEXT section.

    Args:
        historical_context: Summaries of prior investigations, as supplied in
            the input state, most recent first. The caller owns this shape
            entirely — the graph defines it as a loose mapping — so each entry
            is rendered as JSON and truncated rather than read field by field.

    Returns:
        The rendered section, without a trailing newline. ``None`` and ``[]``
        render as ``(none)``: a first-ever investigation is the normal case,
        not a degradation.
    """
    if not historical_context:
        return "HISTORICAL CONTEXT\n(none — no previous investigations were supplied)"

    kept = historical_context[:MAX_HISTORICAL_INVESTIGATIONS]
    rendered: list[str] = []
    for position, investigation in enumerate(kept, start=1):
        text = json.dumps(investigation, indent=2, default=str)
        if len(text) > MAX_HISTORICAL_CHARS:
            text = f"{text[:MAX_HISTORICAL_CHARS]}\n... (truncated)"
        rendered.append(f"[previous investigation {position}]\n{text}")

    if len(historical_context) > len(kept):
        rendered.append(
            f"(and {len(historical_context) - len(kept)} older investigation(s), "
            "omitted)"
        )

    body = "\n".join(rendered)
    return f"HISTORICAL CONTEXT — {len(kept)} of {len(historical_context)}\n{body}"


def build_prepare_output_prompt(
    parser_metrics: ParserMetrics | None,
    statistics: Statistics | None,
    timeline: list[TimelineEvent] | None,
    error_summary: ErrorSummary | None,
    pattern_summary: PatternSummary | None,
    investigation_notes: list[str] | None = None,
    historical_context: list[dict[str, Any]] | None = None,
    *,
    application_name: str | None = None,
    investigation_timestamp: str | None = None,
) -> str:
    """Render the human turn of the synthesis call.

    Section order runs from measurement to inference: parser health, then the
    two deterministic reports, then the two model-derived summaries, then the
    notes and the history. A model reading the facts before the interpretations
    is in a position to disagree with the interpretations, which is what the
    system prompt asks it to do.

    Args:
        parser_metrics: Ingestion health from the parser.
        statistics: The Statistics Node's output.
        timeline: The Timeline Node's output, in chronological order.
        error_summary: The Error Analysis Node's output.
        pattern_summary: The Pattern Analysis Node's output.
        investigation_notes: What every upstream pass recorded about its own
            limits.
        historical_context: Summaries of previous investigations, if any.
        application_name: The application under investigation, included as
            context when the caller supplied one.
        investigation_timestamp: When the investigation was run, included as
            context when the caller supplied one.

    Returns:
        The rendered prompt string. Always well-formed, including when every
        input is empty — the node decides whether an empty payload is worth a
        call, and this function does not second-guess it.
    """
    header_lines = []
    if application_name:
        header_lines.append(f"Application under investigation: {application_name}")
    if investigation_timestamp:
        header_lines.append(f"Investigation timestamp: {investigation_timestamp}")
    header = "\n".join(header_lines) + "\n\n" if header_lines else ""

    return (
        f"{header}"
        "Below is the complete output of one log investigation, ordered from "
        "measurement to inference.\n\n"
        f"{format_parser_health(parser_metrics)}\n\n"
        f"{format_statistics(statistics)}\n\n"
        f"{format_timeline(timeline)}\n\n"
        f"{format_error_summary(error_summary)}\n\n"
        f"{format_pattern_summary(pattern_summary)}\n\n"
        f"{format_investigation_notes(investigation_notes)}\n\n"
        f"{format_historical_context(historical_context)}\n\n"
        "Synthesize this into the investigation's conclusion: the one-sentence "
        "root cause, the multi-paragraph executive summary covering what "
        "happened, how it spread and what limits the conclusion, and your "
        "confidence in the diagnosis."
    )


def prompt_payload_sizes(
    statistics: Statistics | None,
    timeline: list[TimelineEvent] | None,
    error_summary: ErrorSummary | None,
    pattern_summary: PatternSummary | None,
    historical_context: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Counts describing what a prompt for these inputs would carry.

    Exists for the node's log line: "the model saw 12 of 31 signatures and no
    pattern summary" is what makes a thin conclusion diagnosable after the fact.
    """
    signatures = (error_summary or {}).get("signatures") or []
    events = timeline or []
    history = historical_context or []

    return {
        "signatures_total": len(signatures),
        "signatures_sent": min(len(signatures), MAX_ERROR_SIGNATURES),
        "timeline_events": len(events),
        "milestones": sum(
            1 for event in events if event.get("event_type") == "milestone"
        ),
        "anomalies": len((pattern_summary or {}).get("anomalies") or []),
        "loggers": len((statistics or {}).get("logger_distribution") or []),
        "historical_total": len(history),
        "historical_sent": min(len(history), MAX_HISTORICAL_INVESTIGATIONS),
    }

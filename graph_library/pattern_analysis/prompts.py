"""Prompt construction for the Pattern Analysis Node.

The node's inputs are two deterministic payloads that were built for different
readers: ``Statistics`` is a set of distributions, ``timeline`` is an ordered
event log. Neither is written to be read by a model, and the whole of the
timeline will not fit in a prompt for a long incident. This module is the
adapter — it selects what matters, serializes it in a form the model can align
across the two sources, and states the question.

Three decisions shape what gets sent:

    * **JSON, not prose.** The same choice
      :mod:`graph_library.error_analysis.node` makes, for the same reason: the
      model has to echo logger names and timestamps back exactly, and a JSON
      payload keeps the mapping between a value and the thing it describes
      unambiguous.
    * **Milestones are never dropped, buckets are.** A long incident produces
      hundreds of buckets and at most seven milestones, and the milestones are
      the ones that carry the narrative — onset, peak, recovery. When the
      buckets have to be trimmed the busiest are kept, since a quiet window is
      the least informative thing in the series, and the omission is stated in
      the prompt rather than hidden.
    * **Investigation notes are included.** They are where the parser and the
      timeline record what they *could not* do — unparseable lines, entries with
      no timestamp, dropped empty windows. A model reading distributions with no
      idea that a third of the payload never made it into them will overstate
      what the distributions mean.
"""

from __future__ import annotations

import json
from typing import Any

from graph_library.models import Statistics, TimelineEvent

#: The node's standing instructions. Written against the two failure modes a
#: model shows on aggregate log data: narrating the input back ("there were 412
#: errors, mostly from the order service") instead of identifying what is
#: *abnormal* about it, and inventing a cascade between components that merely
#: appear in the same list.
SYSTEM_PROMPT = """\
You are a senior site-reliability engineer studying the behavior of an \
application over one window of time.

You will receive two deterministic reports about the same log payload:
- STATISTICS: what the dataset contains — level and logger distributions, \
severity counts, timestamp coverage, and distributions over metadata fields \
discovered in the logs.
- TIMELINE: how the window unfolded — fixed-width buckets with per-window \
counts, and milestones marking the inflection points (first error, error \
onset, peak error volume, recovery).

Your task is to identify BEHAVIORAL PATTERNS across those two reports. \
Specifically:
1. Cross-logger cascades — a failure in one component followed by failures in \
others, in an order that suggests propagation rather than coincidence.
2. Metadata concentrations — activity or failures clustered in one value of a \
metadata dimension (a single endpoint, tenant, host, scenario or thread).
3. Baseline shifts — a lasting change in the normal operating level, as \
distinct from a transient spike.
4. Error onset behavior — what the buckets around the onset, peak and recovery \
milestones say about how the incident started, escalated and ended.

Rules:
- Report what is ABNORMAL, not what is present. "The order service logged the \
most errors" restates the input; "errors moved from the payment client to the \
order service within one bucket, in that order" is a pattern.
- Ground every claim in the numbers you were given. Do not invent loggers, \
timestamps, counts or metadata values that are not in the input.
- Copy logger names and timestamps EXACTLY as they appear in the input.
- Sequence is evidence for propagation; co-occurrence in a list is not. Two \
components failing in the same bucket is only a cascade if the timeline shows \
one starting before the other.
- An empty `anomalies` list is a valid and expected answer for a payload that \
behaved normally. Do not manufacture an anomaly to fill it.
- Read the INVESTIGATION NOTES for what the deterministic passes could not \
measure. Entries that were dropped are missing from every distribution above, \
and a pattern that depends on them cannot be claimed.\
"""

#: Statistics fields sent to the model, in the order they are rendered. The
#: whole payload, in other words — every field of ``Statistics`` is evidence for
#: at least one of the four pattern kinds the system prompt names.
STATISTICS_FIELDS: tuple[str, ...] = (
    "severity",
    "timestamp_coverage",
    "level_distribution",
    "logger_distribution",
    "metadata_distributions",
)

#: Timeline event fields sent to the model. ``end_timestamp`` is excluded — the
#: window width is constant across a run and repeating it on every bucket costs
#: tokens without telling the model anything the ``timestamp`` sequence does
#: not. ``total_logs`` and ``warning_count`` stay, because a spike in total
#: volume with flat errors is a different pattern from a spike in both.
TIMELINE_FIELDS: tuple[str, ...] = (
    "event_type",
    "milestone_kind",
    "timestamp",
    "total_logs",
    "error_count",
    "warning_count",
    "top_loggers",
    "sample_messages",
    "summary",
)

#: How many bucket events may be sent. A day-long incident at 15-minute
#: granularity produces 96 buckets and a week-long one produces 168 hourly
#: buckets, so the cap is rarely reached; it exists so that a pathological
#: payload cannot push the prompt past a context window. Milestones are exempt —
#: there are at most seven of them and they carry the narrative.
MAX_TIMELINE_BUCKETS = 60

#: How many investigation notes may be sent. They are one line each and there
#: are usually fewer than ten; the cap bounds the one case that is not true — a
#: payload that produced a note per malformed section.
MAX_INVESTIGATION_NOTES = 40


def _select_buckets(buckets: list[TimelineEvent]) -> tuple[list[TimelineEvent], int]:
    """Trim the bucket series to :data:`MAX_TIMELINE_BUCKETS`, busiest first.

    Chronological order is restored after the selection, because the sequence
    is the evidence the model reasons over — a series sorted by volume would
    invite exactly the coincidence-as-causation error the system prompt warns
    against.

    Args:
        buckets: The bucket events, in chronological order.

    Returns:
        A ``(kept, dropped_count)`` pair. ``kept`` is in chronological order and
        ``dropped_count`` is ``0`` whenever the series fit.
    """
    if len(buckets) <= MAX_TIMELINE_BUCKETS:
        return buckets, 0

    # Rank by errors first, then total volume: an incident's shape is carried by
    # its error buckets, and a busy-but-clean window is the next most useful
    # thing to keep. The index tiebreaker keeps the choice deterministic.
    ranked = sorted(
        enumerate(buckets),
        key=lambda pair: (
            -int(pair[1].get("error_count") or 0),
            -int(pair[1].get("total_logs") or 0),
            pair[0],
        ),
    )
    kept = sorted(ranked[:MAX_TIMELINE_BUCKETS], key=lambda pair: pair[0])
    return [bucket for _index, bucket in kept], len(buckets) - MAX_TIMELINE_BUCKETS


def format_statistics(statistics: Statistics | None) -> str:
    """Render the statistics payload as the STATISTICS section of the prompt.

    Args:
        statistics: The Statistics Node's output. ``None`` and ``{}`` render
            identically — as an explicit statement that the section is empty,
            not as an empty JSON object, because "no statistics were produced"
            is a fact the model should be told rather than left to infer.

    Returns:
        The rendered section, without a trailing newline.
    """
    if not statistics:
        return "STATISTICS\n(unavailable — the statistics node produced nothing)"

    payload = {
        field: statistics[field]  # type: ignore[literal-required]
        for field in STATISTICS_FIELDS
        if field in statistics
    }

    return f"STATISTICS\n{json.dumps(payload, indent=2, default=str)}"


def format_timeline(timeline: list[TimelineEvent] | None) -> str:
    """Render the timeline as the TIMELINE section of the prompt.

    Milestones are rendered ahead of the buckets, under their own heading. They
    are the answer to "what happened", the buckets are the supporting series,
    and separating them stops the seven events that matter most from being
    buried in sixty that support them.

    Args:
        timeline: The Timeline Node's output, in chronological order. ``None``
            and ``[]`` render identically, for the same reason as
            :func:`format_statistics`.

    Returns:
        The rendered section, without a trailing newline.
    """
    if not timeline:
        return (
            "TIMELINE\n(unavailable — nothing in the payload could be placed on "
            "a time axis)"
        )

    milestones = [
        event for event in timeline if event.get("event_type") == "milestone"
    ]
    buckets = [event for event in timeline if event.get("event_type") == "bucket"]
    kept, dropped = _select_buckets(buckets)

    def render(events: list[TimelineEvent]) -> str:
        payload = [
            {
                field: event[field]  # type: ignore[literal-required]
                for field in TIMELINE_FIELDS
                if field in event and event[field] is not None  # type: ignore[literal-required]
            }
            for event in events
        ]
        return json.dumps(payload, indent=2, default=str)

    sections = [
        f"TIMELINE — {len(milestones)} milestone(s)",
        render(milestones),
        f"TIMELINE — {len(kept)} time bucket(s), chronological",
    ]

    if dropped:
        # Stated rather than silent: a model told it has the whole series will
        # read a gap between two buckets as a quiet period.
        sections.append(
            f"(the {dropped} lowest-volume bucket(s) were omitted to fit; the "
            "series below is therefore not contiguous)"
        )

    sections.append(render(kept))
    return "\n".join(sections)


def format_investigation_notes(notes: list[str] | None) -> str:
    """Render the notes as the INVESTIGATION NOTES section of the prompt."""
    if not notes:
        return "INVESTIGATION NOTES\n(none)"

    kept = notes[:MAX_INVESTIGATION_NOTES]
    lines = "\n".join(f"- {note}" for note in kept)

    if len(notes) > len(kept):
        lines += f"\n- (and {len(notes) - len(kept)} further note(s), omitted)"

    return f"INVESTIGATION NOTES\n{lines}"


def build_pattern_analysis_prompt(
    statistics: Statistics | None,
    timeline: list[TimelineEvent] | None,
    investigation_notes: list[str] | None = None,
    *,
    application_name: str | None = None,
) -> str:
    """Render the human turn of the pattern-analysis call.

    Args:
        statistics: The Statistics Node's output.
        timeline: The Timeline Node's output, in chronological order.
        investigation_notes: What the deterministic passes recorded about their
            own limits. Included because a distribution says nothing about the
            records that never reached it.
        application_name: The application under investigation, included as
            context when the caller supplied one.

    Returns:
        The rendered prompt string. Always well-formed, including when both
        inputs are empty — the node decides whether an empty payload is worth a
        call, and this function does not second-guess it.
    """
    header = (
        f"Application under investigation: {application_name}\n\n"
        if application_name
        else ""
    )

    return (
        f"{header}"
        "Below are the two deterministic reports for one log payload, followed "
        "by the notes the deterministic passes recorded about their own "
        "limits.\n\n"
        f"{format_statistics(statistics)}\n\n"
        f"{format_timeline(timeline)}\n\n"
        f"{format_investigation_notes(investigation_notes)}\n\n"
        "Identify the behavioral patterns across these two reports: "
        "cross-logger cascades, metadata concentrations, baseline shifts, and "
        "what the onset, peak and recovery milestones say about how the "
        "incident developed."
    )


def prompt_payload_sizes(
    statistics: Statistics | None,
    timeline: list[TimelineEvent] | None,
) -> dict[str, Any]:
    """Counts describing what a prompt for these inputs would carry.

    Exists for the node's log line: "the model saw 3 milestones and 60 of 412
    buckets" is what makes a thin answer diagnosable after the fact.
    """
    events = timeline or []
    buckets = [event for event in events if event.get("event_type") == "bucket"]
    _kept, dropped = _select_buckets(buckets)

    return {
        "milestones": sum(
            1 for event in events if event.get("event_type") == "milestone"
        ),
        "buckets_total": len(buckets),
        "buckets_sent": len(buckets) - dropped,
        "loggers": len((statistics or {}).get("logger_distribution") or []),
        "metadata_keys": len((statistics or {}).get("metadata_distributions") or {}),
    }

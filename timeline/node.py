"""The deterministic Timeline Node for the LogSherlock graph.

The node answers one question about the parser's output — *"how did this
incident unfold over time?"* — and answers it with arithmetic only: no LLM, no
prompts, no network. Given the same ``parsed_logs`` it always returns the same
``timeline``, in the same order, with the same wording.

What it produces:

    * a contiguous series of adaptively sized time **buckets**, each carrying
      volume, severity counts, the loudest loggers and a couple of preview
      messages;
    * the **milestones** that make the shape readable — log coverage
      boundaries, first / last error, and the error onset → peak → recovery
      narrative.

Scope boundaries it deliberately respects:

    * **Timestamps are never repaired.** Entries the parser could not stamp
      (including deliberately-rejected incomplete stamps such as yearless
      syslog) are unbucketable; they are excluded and *reported*, never guessed
      into place.
    * **Dataset composition** (level / logger distributions, coverage ratios)
      belongs to the statistics node and is not mirrored here.
    * **Interpretation** — what the shape *means* — belongs to the LLM analysis
      and recommendation nodes. Every string this node emits is a mechanical
      restatement of a count it computed.

The public entry point is :func:`timeline_node`, whose signature matches the
other graph nodes (full state in, partial state delta out).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from models import ParsedLogEntry, TimelineEvent

from .buckets import (
    TimeBucket,
    bucket_event,
    build_buckets,
    is_error,
    timestamped_entries,
)
from .granularity import describe_duration, select_bucket_size, to_comparable
from .milestones import detect_milestones

#: Tie-break order for milestones that land on the same timestamp. It follows
#: the incident narrative rather than the alphabet, so a payload collapsed into
#: a single instant still reads start → first error → onset → peak →
#: recovery → last error → end.
_MILESTONE_ORDER: tuple[str, ...] = (
    "logs_start",
    "first_error",
    "error_onset",
    "peak_error_volume",
    "recovery_onset",
    "last_error",
    "logs_end",
)

#: Emitted verbatim when nothing in the payload can be placed on a time axis.
NO_TIMESTAMPS_NOTE = (
    "Timeline skipped: No usable timestamps present in parsed logs."
)


def build_timeline(
    parsed_logs: list[ParsedLogEntry],
) -> tuple[list[TimelineEvent], list[str]]:
    """Build the ordered timeline and the notes explaining how it was built.

    Args:
        parsed_logs: Normalized entries from the parser node. Entries without a
            usable timestamp are ignored (and counted by the caller).

    Returns:
        A ``(timeline, notes)`` pair. ``timeline`` is empty and ``notes``
        carries only :data:`NO_TIMESTAMPS_NOTE` when no entry can be placed.
    """
    entries = timestamped_entries(parsed_logs)
    if not entries:
        return [], [NO_TIMESTAMPS_NOTE]

    earliest = entries[0]["timestamp"]
    latest = entries[-1]["timestamp"]
    size = select_bucket_size(to_comparable(latest) - to_comparable(earliest))

    buckets = build_buckets(entries, size)
    populated = [bucket for bucket in buckets if not bucket.is_empty]

    events = [bucket_event(bucket) for bucket in populated]
    events.extend(detect_milestones(entries, buckets, size))

    return _chronological(events), _build_notes(
        entries, buckets, populated_count=len(populated), width=describe_duration(size)
    )


def _build_notes(
    entries: list[ParsedLogEntry],
    buckets: list[TimeBucket],
    *,
    populated_count: int,
    width: str,
) -> list[str]:
    """Explain the choices behind the timeline, in plain language.

    Every note states something a reader could otherwise only discover by
    re-deriving it: the granularity that was selected, the windows that were
    dropped for being empty, and whether the error narrative was available at
    all.
    """
    notes = [
        f"Timeline: bucketed {len(entries)} timestamped "
        f"{_plural(len(entries), 'entry', 'entries')} into {len(buckets)} "
        f"{_plural(len(buckets), 'window', 'windows')} of {width}, spanning "
        f"{entries[0]['timestamp'].isoformat()} to "
        f"{entries[-1]['timestamp'].isoformat()}."
    ]

    empty = len(buckets) - populated_count
    if empty:
        notes.append(
            # A gap only exists when there are at least two windows, so the
            # "windows" plural always holds; only the verb has to agree.
            f"Timeline: {empty} of {len(buckets)} windows contained no logs "
            f"and {_plural(empty, 'was', 'were')} omitted from the event list."
        )

    if not any(is_error(entry) for entry in entries):
        notes.append(
            "Timeline: no error-level entries were found, so the error onset, "
            "peak and recovery milestones were not emitted."
        )
    return notes


def _data_quality_note(missing: int) -> str:
    """Phrase the warning for entries the timeline could not place."""
    if missing == 1:
        return (
            "Data Quality Warning: 1 entry omitted a complete timestamp and "
            "was excluded from timeline analysis."
        )
    return (
        f"Data Quality Warning: {missing} entries omitted complete timestamps "
        "and were excluded from timeline analysis."
    )


def _missing_timestamp_count(state: dict[str, Any]) -> int:
    """Count entries the parser could not stamp.

    ``parser_metrics`` is the machine-readable source of truth and is preferred;
    the fallback recounts from ``parsed_logs`` only when the node is invoked
    without metrics (as in isolated unit tests).
    """
    metrics = state.get("parser_metrics")
    if isinstance(metrics, dict) and "missing_timestamp_lines" in metrics:
        return int(metrics["missing_timestamp_lines"] or 0)
    return sum(
        1
        for entry in state.get("parsed_logs") or []
        if not isinstance(entry.get("timestamp"), datetime)
    )


def _chronological(events: list[TimelineEvent]) -> list[TimelineEvent]:
    """Order events strictly by time, with a fully specified tiebreaker.

    Events sharing an instant are ordered milestones-first (the headline before
    the window that contains it) and then by :data:`_MILESTONE_ORDER`, so the
    output is byte-identical across runs and platforms.

    The sort key round-trips this node's *own* ISO-8601 output back into a
    datetime — that is reading back a canonical serialization, not re-parsing a
    source timestamp, which remains the parser's exclusive job.
    """

    def key(event: TimelineEvent) -> tuple[datetime, int, int]:
        moment = to_comparable(datetime.fromisoformat(event["timestamp"]))
        kind = event.get("milestone_kind")
        if kind is None:
            return moment, 1, 0
        return moment, 0, _MILESTONE_ORDER.index(kind)

    return sorted(events, key=key)


def _plural(count: int, singular: str, plural: str) -> str:
    """Return ``singular`` when ``count == 1`` else ``plural``."""
    return singular if count == 1 else plural


def timeline_node(state: dict[str, Any]) -> dict[str, Any]:
    """Build the chronological timeline from ``parsed_logs``.

    Args:
        state: The LogSherlock graph state. Reads ``parsed_logs`` (the entries
            to place) and ``parser_metrics`` (to report how many entries could
            not be placed). Both are treated as read-only.

    Returns:
        A partial state delta containing exactly:

            * ``timeline`` — ordered :class:`TimelineEvent` records,
            * ``investigation_notes`` — how the timeline was built and what was
              excluded,
            * ``completed_stages`` — ``["timeline"]``.

        No other state field is touched. ``investigation_notes`` and
        ``completed_stages`` use additive reducers in the graph, so returning
        only this node's contributions is correct even though three sibling
        nodes run in the same superstep.
    """
    parsed_logs = state.get("parsed_logs") or []
    timeline, notes = build_timeline(parsed_logs)

    if not timeline:
        # Nothing could be placed on a time axis. The single note already says
        # exactly that, so the data-quality warning would only restate it.
        return {
            "timeline": [],
            "investigation_notes": notes,
            "completed_stages": ["timeline"],
        }

    missing = _missing_timestamp_count(state)
    if missing:
        notes.insert(1, _data_quality_note(missing))

    return {
        "timeline": timeline,
        "investigation_notes": notes,
        "completed_stages": ["timeline"],
    }

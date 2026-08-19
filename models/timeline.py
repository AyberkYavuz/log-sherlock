"""The chronological timeline model produced by the Timeline Node.

A ``TimelineEvent`` answers *"what happened, when?"* for a single point or
window on the investigation's time axis. Two flavours share the one shape,
discriminated by ``event_type``:

    * ``"bucket"`` — a fixed-size time window (the granularity is chosen
      adaptively from the log span) carrying aggregate counts for that window.
      These events, and only these, populate ``end_timestamp``.
    * ``"milestone"`` — a single notable moment (log coverage boundaries, the
      first / last error, the error onset, the error peak, the recovery point).
      These events, and only these, populate ``milestone_kind``.

Every field is plain and JSON-serializable — timestamps cross the node boundary
as ISO-8601 *strings*, not :class:`~datetime.datetime` objects, so the timeline
can be rendered, stored and shipped to an LLM without a serialization layer.

Like every shared model this is a :class:`~typing.TypedDict`, so a
``TimelineEvent`` value *is* the plain dict that flows through LangGraph state.
``total=False`` because the two flavours populate overlapping-but-different
field sets; the Timeline Node nevertheless emits every key on every event (with
``None`` where a field does not apply) so consumers see one stable shape.
"""

from __future__ import annotations

from typing import Literal, TypedDict

#: Which flavour of event this is — see the module docstring.
TimelineEventType = Literal["milestone", "bucket"]

#: The closed vocabulary of inflection points the Timeline Node can emit.
#:
#: ``logs_start`` / ``logs_end``   — first / last usable timestamp in the payload.
#: ``first_error`` / ``last_error`` — first / last entry at an error severity.
#: ``error_onset``                 — the bucket where error volume breaks out of
#:                                   its prior baseline.
#: ``peak_error_volume``           — the bucket with the highest error count.
#: ``recovery_onset``              — the first post-peak bucket back at baseline.
MilestoneKind = Literal[
    "logs_start",
    "logs_end",
    "first_error",
    "last_error",
    "error_onset",
    "peak_error_volume",
    "recovery_onset",
]


class TimelineEvent(TypedDict, total=False):
    """One entry on the investigation timeline.

    Attributes:
        event_type: ``"bucket"`` for an aggregated time window, ``"milestone"``
            for a single notable moment.
        timestamp: ISO-8601 string. For a bucket this is the window's *start*;
            for a milestone it is the moment itself (the source entry's
            timestamp, or the start of the bucket the milestone refers to).
        end_timestamp: ISO-8601 string marking the exclusive end of a bucket
            window. ``None`` on milestone events.
        milestone_kind: Which inflection point this is. ``None`` on bucket
            events.
        total_logs: Number of log entries in the window the event describes.
        error_count: Entries at an error severity within that window.
        warning_count: Entries at a warning severity within that window.
        top_loggers: Up to 3 logger names contributing most to the window,
            ranked by error count then total count.
        sample_messages: Up to 2 representative messages for UI preview, error
            messages first. Long messages are truncated.
        summary: A deterministic, human-readable one-line explanation. Composed
            from the counts above by fixed rules — never by an LLM.
    """

    event_type: TimelineEventType
    timestamp: str
    end_timestamp: str | None
    milestone_kind: MilestoneKind | None
    total_logs: int
    error_count: int
    warning_count: int
    top_loggers: list[str]
    sample_messages: list[str]
    summary: str

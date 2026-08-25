"""Deterministic inflection-point detection for the Timeline Node.

Buckets tell you *how much* happened and *when*; milestones tell you which
moments a human should look at first. Seven kinds are emitted, all derived by
fixed arithmetic over the bucket series and the entry list — no heuristics that
depend on wording, no model, no randomness.

The error-shaped milestones form one narrative:

    ``error_onset``        the moment error volume broke out of its baseline,
    ``peak_error_volume``  the worst bucket of the incident,
    ``recovery_onset``     the first bucket back at that baseline afterwards.

All three are omitted when the payload contains no error-level entries at all —
an absent milestone means "not observed", never "assumed zero".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from graph_library.models import MilestoneKind, ParsedLogEntry, TimelineEvent

from .buckets import (
    TimeBucket,
    is_error,
    is_warning,
    sample_messages,
    top_loggers,
)
from .granularity import describe_duration

#: The multiple of the prior baseline an error count must exceed to count as an
#: onset. With a baseline of zero — no errors seen yet — any error at all
#: clears the bar, which is the "sudden non-zero spike" case.
ONSET_BASELINE_MULTIPLIER = 2


@dataclass(frozen=True)
class ErrorNarrative:
    """The three error-shaped milestones' bucket positions, once resolved.

    Attributes:
        onset_index: First bucket whose error count breaks out of the baseline.
        baseline: Mean errors per bucket *before* the onset (``0.0`` when the
            errors start in the very first bucket).
        peak_index: Bucket with the highest error count; ties go to the earliest.
        recovery_index: First bucket after the peak whose error count is back at
            or below ``baseline``, or ``None`` when errors never subside within
            the observed span.
    """

    onset_index: int
    baseline: float
    peak_index: int
    recovery_index: int | None


def detect_milestones(
    entries: list[ParsedLogEntry],
    buckets: list[TimeBucket],
    bucket_size: timedelta,
) -> list[TimelineEvent]:
    """Emit every milestone the payload supports, in narrative order.

    Args:
        entries: Timestamped entries in chronological order (non-empty).
        buckets: The contiguous bucket series covering those entries.
        bucket_size: The chosen bucket width, used only for phrasing summaries.

    Returns:
        Between two and seven events. ``logs_start`` and ``logs_end`` are always
        present; the error-shaped milestones appear only when the evidence for
        them exists. Final chronological ordering is applied by the caller.
    """
    events = [
        _entry_milestone(
            "logs_start",
            entries[0],
            f"Log coverage starts at {entries[0]['timestamp'].isoformat()}.",
        ),
        _entry_milestone(
            "logs_end",
            entries[-1],
            f"Log coverage ends at {entries[-1]['timestamp'].isoformat()}.",
        ),
    ]

    errors = [entry for entry in entries if is_error(entry)]
    if not errors:
        return events

    events.append(
        _entry_milestone(
            "first_error",
            errors[0],
            f"First error at {errors[0]['timestamp'].isoformat()}"
            f"{_source(errors[0])}.",
        )
    )
    events.append(
        _entry_milestone(
            "last_error",
            errors[-1],
            f"Last error at {errors[-1]['timestamp'].isoformat()}"
            f"{_source(errors[-1])}.",
        )
    )
    events.extend(_narrative_milestones(buckets, bucket_size))
    return events


# ---------------------------------------------------------------------------
# The onset / peak / recovery narrative
# ---------------------------------------------------------------------------


def resolve_narrative(buckets: list[TimeBucket]) -> ErrorNarrative | None:
    """Locate the onset, peak and recovery buckets, or ``None`` if error-free.

    Onset scans forward keeping the running mean of the error counts seen *so
    far*: a bucket is the onset when it contains at least one error and that
    count exceeds :data:`ONSET_BASELINE_MULTIPLIER` times the running mean.
    Because the mean over an error-free prefix is zero, this reduces to "the
    first bucket containing an error" for the common case where the payload
    begins quietly, and to a genuine spike test only once errors are already
    part of the baseline.

    Recovery reuses that same baseline as the "back to normal" threshold, so
    the pair reads symmetrically: volume left the baseline at the onset and
    returned to it at the recovery.
    """
    onset = _find_onset(buckets)
    if onset is None:
        return None
    onset_index, baseline = onset

    peak_index = max(
        range(len(buckets)),
        key=lambda index: (buckets[index].error_count, -index),
    )
    recovery_index = next(
        (
            index
            for index in range(peak_index + 1, len(buckets))
            if buckets[index].error_count <= baseline
        ),
        None,
    )
    return ErrorNarrative(onset_index, baseline, peak_index, recovery_index)


def _find_onset(buckets: list[TimeBucket]) -> tuple[int, float] | None:
    """Return ``(index, baseline)`` for the first breakout bucket, or ``None``."""
    seen_errors = 0
    for index, bucket in enumerate(buckets):
        errors = bucket.error_count
        baseline = seen_errors / index if index else 0.0
        if errors > 0 and errors > ONSET_BASELINE_MULTIPLIER * baseline:
            return index, baseline
        seen_errors += errors
    return None


def _narrative_milestones(
    buckets: list[TimeBucket], bucket_size: timedelta
) -> list[TimelineEvent]:
    """Render the onset / peak / recovery buckets as milestone events."""
    narrative = resolve_narrative(buckets)
    if narrative is None:
        return []

    width = describe_duration(bucket_size)
    onset = buckets[narrative.onset_index]
    peak = buckets[narrative.peak_index]

    events = [
        _bucket_milestone(
            "error_onset",
            onset,
            f"Error onset: {onset.error_count} "
            f"{'error' if onset.error_count == 1 else 'errors'} in the {width} "
            f"window starting {onset.start.isoformat()}, against a prior "
            f"baseline of {narrative.baseline:.2f} errors per {width}.",
        ),
        _bucket_milestone(
            "peak_error_volume",
            peak,
            f"Peak error volume: {peak.error_count} "
            f"{'error' if peak.error_count == 1 else 'errors'} in the {width} "
            f"window starting {peak.start.isoformat()} — the highest of any "
            f"window in the span.",
        ),
    ]

    if narrative.recovery_index is not None:
        recovery = buckets[narrative.recovery_index]
        elapsed = describe_duration(
            (narrative.recovery_index - narrative.peak_index) * bucket_size
        )
        events.append(
            _bucket_milestone(
                "recovery_onset",
                recovery,
                f"Recovery onset: errors fell to {recovery.error_count} in the "
                f"{width} window starting {recovery.start.isoformat()}, "
                f"{elapsed} after the peak and back at the "
                f"{narrative.baseline:.2f} baseline.",
            )
        )
    return events


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------


def _entry_milestone(
    kind: MilestoneKind, entry: ParsedLogEntry, summary: str
) -> TimelineEvent:
    """Build a milestone that points at one specific log entry.

    The count fields describe the window the event refers to — here a window of
    exactly one entry — so a consumer can read counts off any event uniformly.
    """
    return TimelineEvent(
        event_type="milestone",
        timestamp=entry["timestamp"].isoformat(),
        end_timestamp=None,
        milestone_kind=kind,
        total_logs=1,
        error_count=1 if is_error(entry) else 0,
        warning_count=1 if is_warning(entry) else 0,
        top_loggers=top_loggers([entry]),
        sample_messages=sample_messages([entry]),
        summary=summary,
    )


def _bucket_milestone(
    kind: MilestoneKind, bucket: TimeBucket, summary: str
) -> TimelineEvent:
    """Build a milestone that points at one bucket.

    ``end_timestamp`` stays ``None`` even though a bucket has an end: the field
    marks bucket *events*, and this event is a milestone. The window itself is
    still emitted separately as its own bucket event.
    """
    return TimelineEvent(
        event_type="milestone",
        timestamp=bucket.start.isoformat(),
        end_timestamp=None,
        milestone_kind=kind,
        total_logs=bucket.total_logs,
        error_count=bucket.error_count,
        warning_count=bucket.warning_count,
        top_loggers=top_loggers(bucket.entries),
        sample_messages=sample_messages(bucket.entries),
        summary=summary,
    )


def _source(entry: ParsedLogEntry) -> str:
    """Render the "from <logger>" clause, or nothing when there is no logger."""
    logger = entry.get("logger")
    if not isinstance(logger, str) or not logger.strip():
        return ""
    return f" from {logger.strip()}"


__all__ = [
    "ErrorNarrative",
    "ONSET_BASELINE_MULTIPLIER",
    "detect_milestones",
    "resolve_narrative",
]

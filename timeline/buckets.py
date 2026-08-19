"""Bucket construction and aggregation for the Timeline Node.

This module turns a chronologically ordered list of timestamped
``ParsedLogEntry`` dicts into a *contiguous* series of fixed-width
:class:`TimeBucket` objects, and knows how to render one bucket (or one entry)
as a :class:`~models.timeline.TimelineEvent`.

Two rules govern the module:

    * **No re-parsing.** Timestamps arrive as :class:`~datetime.datetime`
      objects normalized by the parser and are used exactly as given; entries
      without a timestamp never reach this module.
    * **Determinism.** Identical input yields identical buckets, identical
      orderings and identical strings — every ranking has an explicit
      tiebreaker, nothing depends on dict or set iteration order.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from models import ParsedLogEntry, TimelineEvent

from .granularity import bucket_index, floor_to_bucket, to_comparable

# -- severity vocabulary -----------------------------------------------------
# The timeline groups the "something is broken" severities together, because
# error onset / peak / recovery are questions about failure volume, not about
# which word a given ecosystem uses for it. This is deliberately *wider* than
# ``stats.ERROR_LEVELS``: Statistics reports dataset composition and keeps
# FATAL and CRITICAL visible as distinct levels, whereas the timeline needs
# them counted as errors to see the incident at all.
ERROR_LEVELS: frozenset[str] = frozenset({"ERROR", "CRITICAL", "FATAL"})
WARNING_LEVELS: frozenset[str] = frozenset({"WARN", "WARNING"})

#: How many logger names a single event may advertise.
TOP_LOGGER_LIMIT = 3

#: How many preview messages a single event may carry.
SAMPLE_MESSAGE_LIMIT = 2

#: Preview messages are truncated to this many characters — a timeline event is
#: a summary, not a log viewer, and a multi-kilobyte stack trace would dominate
#: the payload handed to the downstream nodes.
SAMPLE_MESSAGE_MAX_LENGTH = 200


@dataclass
class TimeBucket:
    """One fixed-width window of the timeline, plus the entries inside it.

    Buckets are built contiguously across the whole span, so a bucket may be
    empty — an empty bucket is meaningful evidence (nothing was logged) and is
    what makes recovery detection possible.
    """

    index: int
    start: datetime
    end: datetime
    entries: list[ParsedLogEntry] = field(default_factory=list)

    @property
    def total_logs(self) -> int:
        return len(self.entries)

    @property
    def error_count(self) -> int:
        return sum(1 for entry in self.entries if is_error(entry))

    @property
    def warning_count(self) -> int:
        return sum(1 for entry in self.entries if is_warning(entry))

    @property
    def is_empty(self) -> bool:
        return not self.entries


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------


def _level(entry: ParsedLogEntry) -> str:
    """Return the entry's level, upper-cased; ``""`` when it has none."""
    level = entry.get("level")
    return level.strip().upper() if isinstance(level, str) else ""


def is_error(entry: ParsedLogEntry) -> bool:
    """Return whether ``entry`` carries an error-class severity."""
    return _level(entry) in ERROR_LEVELS


def is_warning(entry: ParsedLogEntry) -> bool:
    """Return whether ``entry`` carries a warning-class severity."""
    return _level(entry) in WARNING_LEVELS


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def timestamped_entries(parsed_logs: list[ParsedLogEntry]) -> list[ParsedLogEntry]:
    """Select the entries the timeline can place, in chronological order.

    Entries whose ``timestamp`` is ``None`` are *unbucketable* and are dropped
    here — the node never repairs, infers or re-parses a missing timestamp; a
    value the source did not provide stays absent.

    Ordering is by the UTC-projected timestamp, with ``line_number`` as the
    tiebreaker so entries sharing an instant keep their original file order.
    """
    usable = [
        entry for entry in parsed_logs if isinstance(entry.get("timestamp"), datetime)
    ]

    def order(entry: ParsedLogEntry) -> tuple[datetime, int]:
        return to_comparable(entry["timestamp"]), entry.get("line_number", 0)

    return sorted(usable, key=order)


def build_buckets(
    entries: list[ParsedLogEntry], size: timedelta
) -> list[TimeBucket]:
    """Distribute chronologically ordered ``entries`` into contiguous buckets.

    Bucket 0 starts at the boundary at or before the earliest entry (see
    :func:`~timeline.granularity.floor_to_bucket`) and the series runs, with no
    gaps, through the bucket containing the latest entry.

    Args:
        entries: Timestamped entries, already sorted — as returned by
            :func:`timestamped_entries`. Must not be empty.
        size: The bucket width chosen for this span.

    Returns:
        Every bucket in the span, empty ones included.
    """
    anchor = floor_to_bucket(entries[0]["timestamp"], size)
    last_index = bucket_index(entries[-1]["timestamp"], anchor, size)

    buckets = [
        TimeBucket(
            index=index,
            start=anchor + index * size,
            end=anchor + (index + 1) * size,
        )
        for index in range(last_index + 1)
    ]
    for entry in entries:
        buckets[bucket_index(entry["timestamp"], anchor, size)].entries.append(entry)
    return buckets


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def top_loggers(entries: list[ParsedLogEntry]) -> list[str]:
    """Rank the loggers contributing to ``entries``, most significant first.

    Error volume ranks above raw volume: during an incident the component
    producing the failures matters more than the chattiest one. Ties fall back
    to total volume and then to the logger name, so the order never depends on
    iteration order. Entries without a logger contribute to no name.
    """
    totals: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    for entry in entries:
        logger = entry.get("logger")
        if not isinstance(logger, str) or not logger.strip():
            continue
        name = logger.strip()
        totals[name] += 1
        if is_error(entry):
            errors[name] += 1

    ranked = sorted(totals, key=lambda name: (-errors[name], -totals[name], name))
    return ranked[:TOP_LOGGER_LIMIT]


def sample_messages(entries: list[ParsedLogEntry]) -> list[str]:
    """Pick up to two representative messages, errors first.

    Within each group the *earliest* messages win, because the first occurrence
    of a failure is more informative than a later repeat of it.
    """
    errors = [_preview(entry) for entry in entries if is_error(entry)]
    others = [_preview(entry) for entry in entries if not is_error(entry)]
    picked = [text for text in errors + others if text]
    return picked[:SAMPLE_MESSAGE_LIMIT]


def _preview(entry: ParsedLogEntry) -> str:
    """Render one entry's message as a short, single-line preview."""
    message = entry.get("message") or entry.get("raw") or ""
    text = " ".join(str(message).split())
    if len(text) <= SAMPLE_MESSAGE_MAX_LENGTH:
        return text
    return text[: SAMPLE_MESSAGE_MAX_LENGTH - 3].rstrip() + "..."


def count_summary(total: int, errors: int, warnings: int) -> str:
    """Compose the deterministic ``"N logs (E errors, W warnings)"`` phrase."""
    return (
        f"{total} {_plural(total, 'log', 'logs')} "
        f"({errors} {_plural(errors, 'error', 'errors')}, "
        f"{warnings} {_plural(warnings, 'warning', 'warnings')})"
    )


def _plural(count: int, singular: str, plural: str) -> str:
    """Return ``singular`` when ``count == 1`` else ``plural``."""
    return singular if count == 1 else plural


def bucket_event(bucket: TimeBucket) -> TimelineEvent:
    """Render a bucket as a ``"bucket"`` :class:`TimelineEvent`."""
    return TimelineEvent(
        event_type="bucket",
        timestamp=bucket.start.isoformat(),
        end_timestamp=bucket.end.isoformat(),
        milestone_kind=None,
        total_logs=bucket.total_logs,
        error_count=bucket.error_count,
        warning_count=bucket.warning_count,
        top_loggers=top_loggers(bucket.entries),
        sample_messages=sample_messages(bucket.entries),
        summary=count_summary(
            bucket.total_logs, bucket.error_count, bucket.warning_count
        ),
    )

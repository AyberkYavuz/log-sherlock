"""Adaptive bucket-size selection and the time arithmetic behind it.

The Timeline Node never asks the caller how finely to slice time: it derives the
granularity from the span of the logs themselves, so a 30-second burst and a
week-long incident both produce a readable number of buckets.

Everything here is pure, stdlib-only and deterministic — the same span always
yields the same bucket size, and the same timestamp always lands in the same
bucket.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# -- the adaptive granularity table -----------------------------------------
# Boundaries are asymmetric on purpose (``<`` for the first, ``<=`` afterwards),
# so each span belongs to exactly one band:
#
#     ΔT < 5 min          -> 10 seconds
#     5 min <= ΔT <= 1 h  -> 1 minute
#     1 h   <  ΔT <= 24 h -> 15 minutes
#     ΔT > 24 h           -> 1 hour

FINE_SPAN_LIMIT = timedelta(minutes=5)
MEDIUM_SPAN_LIMIT = timedelta(hours=1)
COARSE_SPAN_LIMIT = timedelta(hours=24)

FINE_BUCKET = timedelta(seconds=10)
MEDIUM_BUCKET = timedelta(minutes=1)
COARSE_BUCKET = timedelta(minutes=15)
WIDE_BUCKET = timedelta(hours=1)


def select_bucket_size(span: timedelta) -> timedelta:
    """Return the bucket width to use for a log payload spanning ``span``.

    Args:
        span: ``latest - earliest`` over the usable timestamps. A zero span
            (a single entry, or several sharing one instant) is valid and
            selects the finest granularity.

    Returns:
        The fixed bucket width, per the table at the top of this module.
    """
    if span < FINE_SPAN_LIMIT:
        return FINE_BUCKET
    if span <= MEDIUM_SPAN_LIMIT:
        return MEDIUM_BUCKET
    if span <= COARSE_SPAN_LIMIT:
        return COARSE_BUCKET
    return WIDE_BUCKET


def describe_duration(duration: timedelta) -> str:
    """Render a duration as a short human phrase, e.g. ``"15 minutes"``.

    Used in summaries and investigation notes so a reader can see *why* the
    timeline looks the way it does without decoding a ``timedelta`` repr.
    """
    seconds = int(duration.total_seconds())
    if seconds <= 0:
        return "0 seconds"
    for unit_seconds, singular in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds % unit_seconds == 0:
            count = seconds // unit_seconds
            return f"{count} {singular}{'' if count == 1 else 's'}"
    return f"{seconds} second{'' if seconds == 1 else 's'}"


def to_comparable(value: datetime) -> datetime:
    """Project a timestamp onto a single, comparable UTC axis.

    The parser hands over whatever the source provided, so a payload may mix
    timezone-aware and naive timestamps — comparing those directly raises. A
    naive value is read as UTC (the only assumption available without inventing
    information) and an aware value is converted.

    The result is used *only* as a sort / bucket key: every timestamp the node
    reports is the original value, unmodified.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def floor_to_bucket(value: datetime, size: timedelta) -> datetime:
    """Snap ``value`` down to the nearest bucket boundary of width ``size``.

    Boundaries are aligned to midnight of the value's own day, so buckets land
    on the clock ticks a reader expects (``12:00:00``, ``12:15:00``, ...) rather
    than at an arbitrary offset inherited from the first log line. Every bucket
    width in :func:`select_bucket_size` divides a day evenly, so the alignment
    is exact.

    The returned datetime keeps ``value``'s ``tzinfo``, which is how the node's
    bucket boundaries stay in the same timezone as the logs they describe.
    """
    day_start = value.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start + (value - day_start) // size * size


def bucket_index(value: datetime, anchor: datetime, size: timedelta) -> int:
    """Return the 0-based index of the bucket ``value`` belongs to.

    ``anchor`` is the start of bucket 0. Both arguments are projected onto the
    UTC axis first (see :func:`to_comparable`) so mixed-timezone payloads index
    consistently.
    """
    return (to_comparable(value) - to_comparable(anchor)) // size

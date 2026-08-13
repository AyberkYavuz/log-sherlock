"""The normalized log-entry model shared across the whole graph.

``ParsedLogEntry`` is the single source of truth for a parsed log line. The
parser produces it directly (there is no separate "internal" representation),
and every downstream node — statistics, timeline, recommendation, report —
consumes exactly this shape.

It is a :class:`~typing.TypedDict` rather than a dataclass so that a parsed
entry *is* the plain dict that flows through LangGraph state: no serialization
or ``to_dict()`` layer, one representation everywhere.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict


class ParsedLogEntry(TypedDict):
    """A single normalized log record.

    Every field except ``line_number``, ``raw`` and ``message`` may be ``None``
    when the information cannot be extracted. The parser never invents values —
    an absent or unparseable timestamp is ``None``, not a guess.

    Attributes:
        line_number: 1-based position of the line within the raw log text.
            Preserved across skipped blank/malformed lines so entries stay
            traceable to the source.
        raw: The original, untouched line.
        timestamp: The event time as a normalized :class:`datetime`, or ``None``
            when absent, unparseable, or *incomplete* — a source stamp that
            omits part of the date (e.g. yearless syslog's ``Jan 10 14:52:31``)
            is ``None``, never a value completed from context. May be timezone
            aware or naive depending on what the source provided. Normalization
            happens once, in the parser; downstream nodes must never re-parse
            timestamps.
        level: The upper-cased severity level (e.g. ``"INFO"``), or ``None``.
        logger: The logger / component name, or ``None``.
        message: The human-readable message (falls back to ``raw`` when no more
            specific message field can be isolated).
        metadata: Additional structured fields not part of the common schema
            (e.g. extra JSON keys). Always a dict, possibly empty.
    """

    line_number: int
    raw: str
    timestamp: datetime | None
    level: str | None
    logger: str | None
    message: str
    metadata: dict[str, Any]

"""Core data structures shared across the parser package.

The parser is the single source of truth for every downstream node, so the
shape it produces must be stable and explicit. This module defines:

    * :class:`LogFormat` — the set of formats the parser can recognise,
    * :class:`ParsedLogEntry` — the normalized record every parser emits.

The graph state (see ``graph.py``) types ``parsed_logs`` loosely as
``list[dict[str, Any]]``. We model each entry as a dataclass here for clarity
and testability and expose :meth:`ParsedLogEntry.to_dict` to hand the graph the
plain-dict shape it expects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LogFormat(str, Enum):
    """Log formats the parser knows how to detect and read.

    Inheriting from ``str`` makes the value JSON-serialisable and lets it be
    used directly in ``investigation_notes`` messages without conversion.

    Add a new member here (and a matching parser) to support another format.
    """

    JSON = "json"
    TEXT = "text"


@dataclass
class ParsedLogEntry:
    """A single normalized log record.

    Every field except ``line_number``, ``raw`` and ``message`` may be ``None``
    when the information cannot be extracted from the source line. Parsers must
    never invent values — an absent timestamp is ``None``, not a guess.

    Attributes:
        line_number: 1-based position of the line within the raw log text.
        raw: The original, untouched line.
        message: The human-readable log message (falls back to ``raw`` when no
            more specific message field can be isolated).
        timestamp: The event timestamp as it appeared in the source, or ``None``.
        level: The upper-cased severity level (e.g. ``"INFO"``), or ``None``.
        logger: The logger / component name, or ``None``.
        metadata: Any additional structured fields that are not part of the
            common schema (e.g. extra JSON keys). Always a dict, possibly empty.
    """

    line_number: int
    raw: str
    message: str
    timestamp: str | None = None
    level: str | None = None
    logger: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the plain-dict representation consumed by the graph state."""
        return {
            "line_number": self.line_number,
            "raw": self.raw,
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
            "metadata": self.metadata,
        }

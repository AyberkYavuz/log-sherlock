"""Parser for unstructured / semi-structured plain-text logs.

Rather than one monolithic regex, this module keeps a small, ordered list of
focused patterns (:data:`_LINE_PATTERNS`). Each targets a common layout and is
tried most-specific first. If none match, the line still yields an entry whose
``message`` is the whole line — a plain-text line is never "malformed", it just
carries less structure.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from models import LogFormat, ParsedLogEntry

from .base_parser import BaseParser
from .normalization import normalize_level, normalize_text
from .timestamps import parse_timestamp

# --- reusable sub-patterns -------------------------------------------------
# Kept as named fragments so the full-line patterns below stay readable and the
# vocabulary (which levels / timestamp shapes we recognise) lives in one place.

_LEVELS = (
    "TRACE|DEBUG|INFO|NOTICE|WARNING|WARN|ERROR|ERR|"
    "CRITICAL|CRIT|FATAL|ALERT|EMERGENCY"
)

# PostgreSQL severity keywords. These are *message severities* (LOG, DETAIL,
# HINT, ...) that only make sense inside the PostgreSQL layout, so they are kept
# out of the generic ``_LEVELS`` vocabulary to avoid over-triggering on ordinary
# text. ``DEBUG1``..``DEBUG5`` are Postgres' graded debug levels.
_PG_LEVELS = (
    "LOG|DETAIL|HINT|STATEMENT|CONTEXT|WARNING|ERROR|"
    "FATAL|PANIC|NOTICE|INFO|DEBUG[1-5]?"
)

# ISO-8601 (``2024-01-01T12:00:00.123Z`` / ``2024-01-01 12:00:00,123``) and the
# syslog ``Jan  1 12:00:00`` shape. Anchored where used below.
_TIMESTAMP = (
    r"(?:"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"
    r")"
)

_LVL = rf"(?P<level>{_LEVELS})"
_PG_LVL = rf"(?P<level>{_PG_LEVELS})"
_TS = rf"(?P<ts>{_TIMESTAMP})"
_LOGGER = r"(?P<logger>[\w][\w.\-]*)"
_MSG = r"(?P<msg>.*)"

# Optional structured fields lifted into ``metadata`` when a pattern captures
# them. ``$`` is allowed in the Spring logger for inner-class names.
_PID = r"(?P<pid>\d+)"  # process id (e.g. Spring / PostgreSQL)
_THREAD = r"\[\s*(?P<thread>[^\]]*?)\s*\]"  # ``[nio-8080-exec-2]`` / ``[   main]``
_TZ = r"(?P<tz>[A-Z][A-Za-z0-9/+\-]{1,5})"  # timezone abbreviation (e.g. ``UTC``)
_SPRING_LOGGER = r"(?P<logger>[\w$][\w.$\-]*)"

# Ordered from most to least specific. The first match wins. The leading
# entries are richer, format-shaped patterns (Spring Boot, PostgreSQL, Python
# logging); the trailing entries are the increasingly generic fallbacks.
_LINE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Spring Boot: TS LEVEL pid --- [thread] logger : message
        rf"^{_TS}\s+{_LVL}\s+{_PID}\s+---\s+{_THREAD}\s+"
        rf"{_SPRING_LOGGER}\s*:\s+{_MSG}$",
        # PostgreSQL: TS TZ [pid] SEVERITY: message
        rf"^{_TS}\s+{_TZ}\s+\[{_PID}\]\s+{_PG_LVL}\s*:\s*{_MSG}$",
        # LEVEL:logger:message  (Python logging default)
        rf"^{_LVL}:{_LOGGER}:{_MSG}$",
        # TS - logger - LEVEL - message
        rf"^{_TS}\s+-\s+{_LOGGER}\s+-\s+{_LVL}\s+-\s+{_MSG}$",
        # TS [LEVEL] logger - message
        rf"^{_TS}\s+\[{_LVL}\]\s+{_LOGGER}\s+-\s+{_MSG}$",
        # TS LEVEL logger: message
        rf"^{_TS}\s+{_LVL}\s+{_LOGGER}:\s*{_MSG}$",
        # TS [LEVEL] message   /   TS LEVEL message
        rf"^{_TS}\s+\[?{_LVL}\]?\s+{_MSG}$",
        # [TS] [LEVEL] message   /   [TS] LEVEL message
        rf"^\[{_TS}\]\s+\[?{_LVL}\]?\s*{_MSG}$",
        # TS message   (timestamped, no level)
        rf"^{_TS}\s+{_MSG}$",
        # [LEVEL] message   /   LEVEL message
        rf"^\[?{_LVL}\]?\s+{_MSG}$",
    )
)

# Named groups that, when a pattern captures them, are lifted into ``metadata``.
# Only values the line actually carries are recorded — the parser never invents
# data. Future formats extend metadata simply by adding a mapping here.
_METADATA_GROUPS: tuple[tuple[str, str], ...] = (
    ("pid", "pid"),
    ("thread", "thread"),
    ("tz", "timezone"),
)
# Groups whose captured text is numeric and stored as an ``int``.
_INT_METADATA_GROUPS = frozenset({"pid"})

# Fallback used to still salvage a leading timestamp / level when no full
# pattern matches, so structure is extracted opportunistically.
_LEADING_TS = re.compile(rf"^{_TS}\b", re.IGNORECASE)
_LEADING_LEVEL = re.compile(rf"^\[?{_LVL}\]?\b", re.IGNORECASE)


class PlainTextParser(BaseParser):
    """Best-effort parser for free-form text logs.

    Acts as the universal fallback: it reports a small non-zero confidence for
    any non-empty input so it wins only when no structured parser recognises
    the logs.
    """

    log_format = LogFormat.TEXT

    #: Baseline confidence. Low enough to lose to a real structured match, high
    #: enough to beat a parser that returns 0.0 for unrecognised input.
    _BASELINE_CONFIDENCE = 0.1

    def confidence(self, sample_lines: Sequence[str]) -> float:
        """Return a low baseline for any non-empty sample (fallback parser)."""
        return self._BASELINE_CONFIDENCE if sample_lines else 0.0

    def parse_line(self, line_number: int, raw: str) -> ParsedLogEntry | None:
        """Parse one text line; always succeeds for non-empty input."""
        text = raw.strip()
        for pattern in _LINE_PATTERNS:
            match = pattern.match(text)
            if match:
                return self._entry_from_match(line_number, raw, match)
        return self._entry_best_effort(line_number, raw, text)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _entry_from_match(
        line_number: int, raw: str, match: re.Match[str]
    ) -> ParsedLogEntry:
        """Build an entry from a named-group regex match."""
        groups = match.groupdict()
        message = normalize_text(groups.get("msg")) or raw
        return ParsedLogEntry(
            line_number=line_number,
            raw=raw,
            timestamp=parse_timestamp(groups.get("ts")),
            level=normalize_level(groups.get("level")),
            logger=normalize_text(groups.get("logger")),
            message=message,
            metadata=PlainTextParser._metadata_from_match(groups),
        )

    @staticmethod
    def _metadata_from_match(groups: dict[str, str | None]) -> dict[str, Any]:
        """Lift recognised optional groups into a ``metadata`` dict.

        Only groups the matched pattern actually captured (present and
        non-blank) are recorded; nothing is invented. Numeric groups such as
        ``pid`` are stored as ``int``.
        """
        metadata: dict[str, Any] = {}
        for group_name, meta_key in _METADATA_GROUPS:
            value = groups.get(group_name)
            if value is None:
                continue
            value = value.strip()
            if not value:
                continue
            metadata[meta_key] = (
                int(value) if group_name in _INT_METADATA_GROUPS else value
            )
        return metadata

    @staticmethod
    def _entry_best_effort(line_number: int, raw: str, text: str) -> ParsedLogEntry:
        """Salvage a leading timestamp/level; keep the remainder as message."""
        timestamp_str: str | None = None
        level: str | None = None
        remainder = text

        ts_match = _LEADING_TS.match(remainder)
        if ts_match:
            timestamp_str = ts_match.group("ts")
            remainder = remainder[ts_match.end():].lstrip()

        level_match = _LEADING_LEVEL.match(remainder)
        if level_match:
            level = normalize_level(level_match.group("level"))
            remainder = remainder[level_match.end():].lstrip()

        return ParsedLogEntry(
            line_number=line_number,
            raw=raw,
            timestamp=parse_timestamp(timestamp_str),
            level=level,
            logger=None,
            message=normalize_text(remainder) or raw,
            metadata={},
        )

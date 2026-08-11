"""Parser for unstructured / semi-structured plain-text logs.

Rather than one monolithic regex, this parser is a small engine over an ordered
registry of focused, declarative patterns (:data:`parser.patterns.LINE_PATTERNS`).
Each :class:`~parser.patterns.LinePattern` targets a common layout (Spring Boot,
PostgreSQL, Python logging, FastAPI/Uvicorn, NestJS, SQL Server, ...) and is
tried most-specific first. If none match, the line still yields an entry whose
``message`` is the whole line — a plain-text line is never "malformed", it just
carries less structure.

New ecosystems are added by extending the registry in :mod:`parser.patterns`;
this engine does not change.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from models import LogFormat, ParsedLogEntry

from .base_parser import BaseParser
from .normalization import normalize_level, normalize_text
from .patterns import (
    LEADING_LEVEL,
    LEADING_TIMESTAMP,
    LINE_PATTERNS,
    LinePattern,
)
from .timestamps import parse_timestamp


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
        for pattern in LINE_PATTERNS:
            match = pattern.regex.match(text)
            if match:
                return self._entry_from_match(line_number, raw, pattern, match)
        return self._entry_best_effort(line_number, raw, text)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _entry_from_match(
        line_number: int,
        raw: str,
        pattern: LinePattern,
        match: re.Match[str],
    ) -> ParsedLogEntry:
        """Build an entry from a matched :class:`LinePattern`.

        ``logger`` and ``message`` come from the pattern's declarations when it
        provides them (a fixed logger for the format, or a builder that
        assembles the message from several groups), otherwise from the ``logger``
        / ``msg`` groups. Metadata is lifted per the pattern's declared fields.
        """
        groups = match.groupdict()

        logger = pattern.logger
        if logger is None:
            logger = normalize_text(groups.get("logger"))

        if pattern.message is not None:
            message = pattern.message(groups)
        else:
            message = normalize_text(groups.get("msg"))
        message = message or raw

        return ParsedLogEntry(
            line_number=line_number,
            raw=raw,
            timestamp=parse_timestamp(groups.get("ts")),
            level=normalize_level(groups.get("level")),
            logger=logger,
            message=message,
            metadata=PlainTextParser._metadata_from_match(pattern, groups),
        )

    @staticmethod
    def _metadata_from_match(
        pattern: LinePattern, groups: dict[str, str | None]
    ) -> dict[str, Any]:
        """Lift the pattern's declared groups into a ``metadata`` dict.

        Only groups the match actually captured (present and non-blank) are
        recorded; nothing is invented. Each field's ``cast`` is applied
        defensively — an unconvertible value is kept as its original string
        rather than raising, honouring the parser's never-raise contract.

        A pattern whose metadata keys are not known up front (a run of
        ``key=value`` tokens, say) supplies an ``extra_metadata`` builder; its
        result is merged on top of the declared fields.
        """
        metadata: dict[str, Any] = {}
        for spec in pattern.metadata:
            value = groups.get(spec.group)
            if value is None:
                continue
            value = value.strip()
            if not value:
                continue
            metadata[spec.key] = _coerce(value, spec.cast)
        if pattern.extra_metadata is not None:
            metadata.update(pattern.extra_metadata(groups))
        return metadata

    @staticmethod
    def _entry_best_effort(line_number: int, raw: str, text: str) -> ParsedLogEntry:
        """Salvage a leading timestamp/level; keep the remainder as message."""
        timestamp_str: str | None = None
        level: str | None = None
        remainder = text

        ts_match = LEADING_TIMESTAMP.match(remainder)
        if ts_match:
            timestamp_str = ts_match.group("ts")
            remainder = remainder[ts_match.end():].lstrip()

        level_match = LEADING_LEVEL.match(remainder)
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


def _coerce(value: str, cast: Any) -> Any:
    """Apply ``cast`` to ``value``, falling back to the raw string on failure."""
    try:
        return cast(value)
    except (ValueError, TypeError):
        return value

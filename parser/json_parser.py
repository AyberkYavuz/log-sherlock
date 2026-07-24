"""Parser for JSON Lines logs (one JSON object per line)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from models import LogFormat, ParsedLogEntry

from .base_parser import BaseParser
from .normalization import (
    LEVEL_KEYS,
    LOGGER_KEYS,
    MESSAGE_KEYS,
    TIMESTAMP_KEYS,
    first_present,
    normalize_json_level,
    normalize_text,
)
from .timestamps import parse_timestamp


class JSONLinesParser(BaseParser):
    """Parse logs where each line is a standalone JSON object.

    Known fields (timestamp, level, logger, message) are pulled out via the
    alias tables in :mod:`parser.normalization`; every remaining key is kept
    verbatim in ``metadata`` so no information is lost. Numeric levels from the
    Pino/Bunyan family (``30`` → ``INFO``) are mapped to names via
    :func:`~parser.normalization.normalize_json_level`.
    """

    log_format = LogFormat.JSON

    def confidence(self, sample_lines: Sequence[str]) -> float:
        """Return the fraction of sample lines that are valid JSON objects.

        Only JSON *objects* count — a bare array or scalar (e.g. ``42``) is not
        a log record, so those do not raise our confidence.
        """
        if not sample_lines:
            return 0.0
        valid = sum(1 for line in sample_lines if self._is_json_object(line))
        return valid / len(sample_lines)

    def parse_line(self, line_number: int, raw: str) -> ParsedLogEntry | None:
        """Parse one JSON object line, or return ``None`` if it is malformed."""
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(record, dict):
            # Valid JSON, but not a log record (array/scalar) — skip it.
            return None
        return self._build_entry(line_number, raw, record)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _is_json_object(line: str) -> bool:
        """Return whether ``line`` parses to a JSON object."""
        try:
            return isinstance(json.loads(line), dict)
        except (json.JSONDecodeError, ValueError):
            return False

    @staticmethod
    def _build_entry(
        line_number: int, raw: str, record: dict[str, Any]
    ) -> ParsedLogEntry:
        """Map a decoded JSON record onto the normalized entry schema."""
        ts_key, ts_value = first_present(record, TIMESTAMP_KEYS)
        level_key, level_value = first_present(record, LEVEL_KEYS)
        logger_key, logger_value = first_present(record, LOGGER_KEYS)
        msg_key, msg_value = first_present(record, MESSAGE_KEYS)

        # Everything we did not lift into a first-class field is metadata.
        consumed = {k for k in (ts_key, level_key, logger_key, msg_key) if k}
        metadata = {k: v for k, v in record.items() if k not in consumed}

        message = normalize_text(msg_value)
        if message is None:
            # No recognised message field: fall back to the raw line so the
            # entry is never empty, without inventing a fake message.
            message = raw

        return ParsedLogEntry(
            line_number=line_number,
            raw=raw,
            timestamp=parse_timestamp(ts_value),
            level=normalize_json_level(level_value),
            logger=normalize_text(logger_value),
            message=message,
            metadata=metadata,
        )

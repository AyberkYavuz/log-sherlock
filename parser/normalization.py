"""Small, pure normalization helpers shared by the concrete parsers.

Keeping these here (rather than inside a specific parser) means both the JSON
and text parsers normalize levels, loggers and messages identically, so
downstream nodes see one consistent vocabulary regardless of source format.
"""

from __future__ import annotations

from typing import Any

# Field-name aliases used when reading loosely-structured records (e.g. JSON
# logs from different emitters). Order is irrelevant; the first key present in
# the record wins. Comparison is case-insensitive (see :func:`first_present`).
TIMESTAMP_KEYS: tuple[str, ...] = (
    "timestamp",
    "time",
    "ts",
    "@timestamp",
    "datetime",
    "date",
    "asctime",
    "eventtime",
)
LEVEL_KEYS: tuple[str, ...] = (
    "level",
    "levelname",
    "severity",
    "loglevel",
    "lvl",
    "log_level",
)
LOGGER_KEYS: tuple[str, ...] = (
    "logger",
    "logger_name",
    "name",
    "module",
    "source",
    "component",
    "channel",
)
MESSAGE_KEYS: tuple[str, ...] = (
    "message",
    "msg",
    "text",
    "log",
    "event",
    "body",
)


def normalize_level(value: Any) -> str | None:
    """Normalize a raw level value to a trimmed, upper-cased string.

    Returns ``None`` for values that carry no level information (``None`` or a
    blank string). Non-string values are coerced via ``str`` so numeric levels
    (e.g. syslog integers) are preserved verbatim rather than dropped.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text.upper() or None


def normalize_text(value: Any) -> str | None:
    """Trim a scalar to a non-empty string, or ``None`` if it is blank/missing."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def first_present(record: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, Any]:
    """Return ``(matched_key, value)`` for the first alias found in ``record``.

    Matching is case-insensitive so ``"Timestamp"`` and ``"timestamp"`` are
    treated the same. Returns ``(None, None)`` when no alias is present.
    """
    lowered = {k.lower(): k for k in record}
    for alias in keys:
        actual = lowered.get(alias)
        if actual is not None:
            return actual, record[actual]
    return None, None

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

# Numeric severity scale used by the Bunyan/Pino family of JSON loggers, which
# emit ``level`` as an integer rather than a name. These values (10..60) never
# collide with syslog's 0..7 scale, so mapping them to names is unambiguous;
# any other numeric level is preserved verbatim by :func:`normalize_level`.
NUMERIC_LEVEL_NAMES: dict[int, str] = {
    10: "TRACE",
    20: "DEBUG",
    30: "INFO",
    40: "WARN",
    50: "ERROR",
    60: "FATAL",
}


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


def normalize_json_level(value: Any) -> str | None:
    """Normalize a JSON log level, mapping numeric Bunyan/Pino levels to names.

    Behaves like :func:`normalize_level` for named levels, but recognises the
    integer severity scale used by JSON loggers such as Pino/Bunyan
    (``30`` → ``"INFO"``, ``50`` → ``"ERROR"``; see :data:`NUMERIC_LEVEL_NAMES`).
    An integer outside that scale is preserved verbatim (as a string) rather
    than guessed at — the parser never invents a level it cannot map.
    """
    number = _as_int(value)
    if number is not None and number in NUMERIC_LEVEL_NAMES:
        return NUMERIC_LEVEL_NAMES[number]
    return normalize_level(value)


def _as_int(value: Any) -> int | None:
    """Return ``value`` as an ``int`` if it *is* an integer, else ``None``.

    ``bool`` is rejected (it is an ``int`` subclass but not a log level), and a
    non-integral float or a non-numeric string yields ``None``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("+-").isdigit():
            return int(text)
    return None


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

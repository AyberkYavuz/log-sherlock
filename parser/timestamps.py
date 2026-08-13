"""Deterministic timestamp normalization for the parser.

The parser is the *only* place timestamps are parsed. Every downstream node
receives ready-to-use :class:`datetime` objects (or ``None``) and must never
re-parse strings.

Supported formats (standard library only):

    ISO 8601 (via :meth:`datetime.datetime.fromisoformat`, Python 3.11+):
        * ``2024-01-01T12:30:45Z``            (UTC "Z" suffix → aware)
        * ``2024-01-01T12:30:45+03:00``       (explicit offset → aware)
        * ``2024-01-01 12:30:45``             (space separator → naive)
        * ``2024-01-01 12:30:45.123``         (fractional seconds → naive)

    US locale-style (12-hour clock, e.g. NestJS' default logger):
        * ``07/22/2026, 10:15:30 AM``

Notes:
    * Only *complete* timestamps are normalized. A value that omits a component
      of the date — notably the syslog (RFC 3164) shape ``Jan 10 14:52:31``,
      which carries no year — yields ``None``, because the parser never invents
      the part the source did not provide. Left to the standard library such a
      value would silently acquire ``strptime``'s default year (1900), which
      reads downstream as a real event time rather than as missing information.
    * Anything unrecognised (including numeric epochs) returns ``None``. The
      function never raises: one bad timestamp must not stop the investigation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Non-ISO formats attempted in order, each via ``datetime.strptime``. Kept as a
# list so adding a format later is a one-line change. ISO 8601 is handled
# separately by ``fromisoformat`` (it covers all the ISO variants above).
#
# Every format here MUST describe a complete date and time. ``strptime`` fills
# any component the format omits from its default epoch (1900-01-01), so a
# yearless format such as syslog's ``"%b %d %H:%M:%S"`` would not report the
# missing year — it would fabricate one. Such formats are deliberately absent:
# the shapes they cover fall through to ``None``, which is how the parser says
# "the source did not provide this".
_STRPTIME_FORMATS: tuple[str, ...] = (
    "%m/%d/%Y, %I:%M:%S %p",  # NestJS logger: "07/22/2026, 10:15:30 AM"
)


def parse_timestamp(value: Any) -> datetime | None:
    """Normalize a raw timestamp value to a :class:`datetime`, or ``None``.

    Accepts an already-``datetime`` value (returned as-is), or a string in any
    supported format. Missing, blank, incomplete (e.g. yearless syslog stamps)
    or unrecognised values yield ``None``. Deterministic and exception-free by
    contract, and never derives a missing component from context.

    Args:
        value: The raw timestamp from a log record (str, datetime, or None).

    Returns:
        A parsed :class:`datetime`, or ``None`` if it cannot be normalized.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value

    text = str(value).strip()
    if not text:
        return None

    return _parse_iso(text) or _parse_strptime(text)


def _parse_iso(text: str) -> datetime | None:
    """Parse an ISO 8601 string, or return ``None``."""
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_strptime(text: str) -> datetime | None:
    """Try each non-ISO format in :data:`_STRPTIME_FORMATS`, or return ``None``.

    Runs of whitespace are collapsed first so a value padded with extra spaces
    still matches a single-space format string.
    """
    normalized = " ".join(text.split())
    for fmt in _STRPTIME_FORMATS:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None

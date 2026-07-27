"""Declarative line-pattern registry for the plain-text parser.

The plain-text parser recognises a growing set of production log ecosystems
(Spring Boot, PostgreSQL, Python logging, FastAPI/Uvicorn, NestJS, SQL Server,
...). Rather than one monolithic regex — or a soup of ad-hoc ones — each
ecosystem is described *declaratively* as one or more :class:`LinePattern`
objects:

    * a regex built from small, reusable fragments (:data:`_TS`, :data:`_LVL`,
      ...), so the vocabulary of timestamps / levels lives in one place;
    * how to turn the match into a normalized entry — a fixed ``logger`` for the
      format (when the line itself carries none), an optional ``message``
      builder (for formats whose message is assembled from several groups), and
      the named groups that should be lifted into ``metadata``.

The engine that consumes this registry lives in :mod:`parser.text_parser`; it
never needs to change. Adding the next ecosystem (Nginx, Redis, Docker, ...) is
a matter of appending a section here: define any new fragments it needs and one
or more :class:`LinePattern` s, ordered most-specific first.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

# A raw match's named groups, as returned by ``re.Match.groupdict()``.
Groups = Mapping[str, "str | None"]


@dataclass(frozen=True)
class MetaField:
    """One named regex group lifted into an entry's ``metadata``.

    Attributes:
        group: Name of the capture group to read.
        key: Key to store the value under in ``metadata``.
        cast: Converter applied to the captured (stripped) string, e.g. ``int``
            for numeric fields. Applied defensively — a value that cannot be
            converted is kept as the original string rather than raising.
    """

    group: str
    key: str
    cast: Callable[[str], Any] = str


@dataclass(frozen=True)
class LinePattern:
    """A single ordered text pattern plus how to normalize its match.

    Attributes:
        regex: Compiled pattern with named groups. ``ts``, ``level``, ``logger``
            and ``msg`` are recognised by convention (mirrors the JSON parser's
            field vocabulary); any additional groups are surfaced via
            :attr:`metadata`.
        logger: A fixed logger name for the format, used when the line itself
            carries no logger token (e.g. ``"uvicorn.access"``). When ``None``
            the logger is taken from the ``logger`` group, if any.
        message: Optional builder that assembles the human-readable message from
            the match's groups. When ``None`` the ``msg`` group is used.
        metadata: Named groups to lift into ``metadata`` (only those the match
            actually captured are recorded — nothing is invented).
    """

    regex: re.Pattern[str]
    logger: str | None = None
    message: Callable[[Groups], str | None] | None = None
    metadata: tuple[MetaField, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Reusable regex fragments. Named so the full-line patterns below stay readable
# and each vocabulary (levels, timestamp shapes, ...) is defined exactly once.
# ---------------------------------------------------------------------------

_LEVELS = (
    "TRACE|DEBUG|INFO|NOTICE|WARNING|WARN|ERROR|ERR|"
    "CRITICAL|CRIT|FATAL|ALERT|EMERGENCY"
)

# PostgreSQL *message severities* (LOG, DETAIL, HINT, ...). Kept out of the
# generic ``_LEVELS`` vocabulary so they only trigger inside the PostgreSQL
# layout. ``DEBUG1``..``DEBUG5`` are Postgres' graded debug levels.
_PG_LEVELS = (
    "LOG|DETAIL|HINT|STATEMENT|CONTEXT|WARNING|ERROR|"
    "FATAL|PANIC|NOTICE|INFO|DEBUG[1-5]?"
)

# NestJS' default logger uses ``LOG`` as its info-level label, so it needs its
# own vocabulary rather than the generic ``_LEVELS``.
_NEST_LEVELS = "LOG|ERROR|WARN|DEBUG|VERBOSE|FATAL"

# ISO-8601 (``2024-01-01T12:00:00.123Z`` / ``2024-01-01 12:00:00,123``) and the
# syslog ``Jan  1 12:00:00`` shape. Anchored where used below.
_TIMESTAMP = (
    r"(?:"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"
    r")"
)

# NestJS' US-locale timestamp: ``07/22/2026, 10:15:30 AM``.
_NEST_TIMESTAMP = r"\d{2}/\d{2}/\d{4},\s+\d{1,2}:\d{2}:\d{2}\s+[AP]M"

_LVL = rf"(?P<level>{_LEVELS})"
_PG_LVL = rf"(?P<level>{_PG_LEVELS})"
_NEST_LVL = rf"(?P<level>{_NEST_LEVELS})"
_TS = rf"(?P<ts>{_TIMESTAMP})"
_LOGGER = r"(?P<logger>[\w][\w.\-]*)"
_MSG = r"(?P<msg>.*)"

# Optional structured fields lifted into ``metadata`` when a pattern captures
# them. ``$`` is allowed in the Spring logger for inner-class names.
_PID = r"(?P<pid>\d+)"  # process id (e.g. Spring / PostgreSQL)
_THREAD = r"\[\s*(?P<thread>[^\]]*?)\s*\]"  # ``[nio-8080-exec-2]`` / ``[   main]``
_TZ = r"(?P<tz>[A-Z][A-Za-z0-9/+\-]{1,5})"  # timezone abbreviation (e.g. ``UTC``)
_SPRING_LOGGER = r"(?P<logger>[\w$][\w.$\-]*)"

# FastAPI / Uvicorn access-log building blocks.
# Uvicorn's own emitter writes timestampless lines (``INFO:     ...``), but real
# deployments (and the logsherlock-benchmarks fixtures) front every line with a
# timestamp (``2026-07-27 14:02:51 INFO:     ...``). ``_OPT_TS`` is an optional
# leading timestamp so a single pattern handles both shapes; the ``ts`` group is
# simply absent (``None``) for the bare form.
_OPT_TS = rf"(?:{_TS}\s+)?"
_CLIENT = r"(?P<client_ip>[0-9a-fA-F:.]+):(?P<client_port>\d+)"  # ``127.0.0.1:53122``
_HTTP_METHOD = r"(?P<method>[A-Z]+)"
_HTTP_PATH = r"(?P<path>\S+)"
_HTTP_PROTOCOL = r"(?P<protocol>HTTP/\d(?:\.\d)?)"
_HTTP_STATUS = r"(?P<status_code>\d{3})"

# SQL Server ERRORLOG error header: ``Error: 1205, Severity: 13, State: 51.``
# The whole header is captured inside ``msg`` (so the message stays intact) while
# its parts are also surfaced as metadata and its presence marks the line ERROR.
_MSSQL_ERROR = (
    r"(?P<level>Error):\s*(?P<error_number>\d+),\s*"
    r"Severity:\s*(?P<severity>\d+),\s*State:\s*(?P<state>\d+)\.?\s*"
)


# ---------------------------------------------------------------------------
# Reusable metadata-field declarations shared across formats.
# ---------------------------------------------------------------------------

_PID_META = MetaField("pid", "pid", int)
_THREAD_META = MetaField("thread", "thread")
_TZ_META = MetaField("tz", "timezone")


# ---------------------------------------------------------------------------
# Message builders for formats whose message is assembled from several groups.
# ---------------------------------------------------------------------------

def _uvicorn_access_message(groups: Groups) -> str:
    """Render an Uvicorn access line as ``GET /health HTTP/1.1 -> 200 OK``."""
    request = f"{groups['method']} {groups['path']} {groups['protocol']}"
    reason = (groups.get("status_reason") or "").strip()
    status = " ".join(part for part in (groups["status_code"], reason) if part)
    return f"{request} -> {status}"


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile one full-line pattern (case-insensitive, like all the others)."""
    return re.compile(pattern, re.IGNORECASE)


# ---------------------------------------------------------------------------
# The ordered pattern registry: most-specific (richest, format-shaped) first,
# down to the increasingly generic fallbacks. The first match wins.
# ---------------------------------------------------------------------------

LINE_PATTERNS: tuple[LinePattern, ...] = (
    # -- Spring Boot: TS LEVEL pid --- [thread] logger : message ------------
    LinePattern(
        _compile(
            rf"^{_TS}\s+{_LVL}\s+{_PID}\s+---\s+{_THREAD}\s+"
            rf"{_SPRING_LOGGER}\s*:\s+{_MSG}$"
        ),
        metadata=(_PID_META, _THREAD_META),
    ),
    # -- PostgreSQL: TS TZ [pid] SEVERITY: message -------------------------
    LinePattern(
        _compile(rf"^{_TS}\s+{_TZ}\s+\[{_PID}\]\s+{_PG_LVL}\s*:\s*{_MSG}$"),
        metadata=(_PID_META, _TZ_META),
    ),
    # -- Python logging default: LEVEL:logger:message ----------------------
    LinePattern(_compile(rf"^{_LVL}:{_LOGGER}:{_MSG}$")),
    # -- FastAPI / Uvicorn access: [TS] LEVEL: ip:port - "METHOD path PROTO" code
    LinePattern(
        _compile(
            rf'^{_OPT_TS}{_LVL}:\s+{_CLIENT}\s+-\s+"{_HTTP_METHOD}\s+{_HTTP_PATH}\s+'
            rf'{_HTTP_PROTOCOL}"\s+{_HTTP_STATUS}(?:\s+(?P<status_reason>.*))?$'
        ),
        logger="uvicorn.access",
        message=_uvicorn_access_message,
        metadata=(
            MetaField("client_ip", "client_ip"),
            MetaField("client_port", "client_port", int),
            MetaField("method", "method"),
            MetaField("path", "path"),
            MetaField("status_code", "status_code", int),
        ),
    ),
    # -- NestJS: [Nest] pid - TS LEVEL [context] message -------------------
    LinePattern(
        _compile(
            rf"^\[Nest\]\s+{_PID}\s+-\s+(?P<ts>{_NEST_TIMESTAMP})\s+"
            rf"{_NEST_LVL}\s+\[(?P<logger>[^\]]+)\]\s+{_MSG}$"
        ),
        metadata=(_PID_META,),
    ),
    # -- SQL Server ERRORLOG: TS (Server|spidNN) [Error: ...] message ------
    LinePattern(
        _compile(
            rf"^{_TS}\s+(?:Server|spid(?P<spid>\d+))\s+"
            rf"(?P<msg>(?:{_MSSQL_ERROR})?.*)$"
        ),
        metadata=(
            MetaField("spid", "spid", int),
            MetaField("error_number", "error_number", int),
            MetaField("severity", "severity", int),
            MetaField("state", "state", int),
        ),
    ),
    # -- Level-prefixed line: [TS] LEVEL:  message -------------------------
    # FastAPI/Uvicorn startup, shutdown and exception lines land here (the
    # padded colon after the level is the tell). The optional leading timestamp
    # covers timestamp-fronted deployments; the bare form keeps working. Generic
    # enough to reuse for any "LEVEL: message" emitter, so no logger is invented.
    LinePattern(_compile(rf"^{_OPT_TS}{_LVL}:\s+{_MSG}$")),
    # -- Generic fallbacks (unchanged), most to least specific -------------
    # TS - logger - LEVEL - message
    LinePattern(_compile(rf"^{_TS}\s+-\s+{_LOGGER}\s+-\s+{_LVL}\s+-\s+{_MSG}$")),
    # TS [LEVEL] logger - message
    LinePattern(_compile(rf"^{_TS}\s+\[{_LVL}\]\s+{_LOGGER}\s+-\s+{_MSG}$")),
    # TS LEVEL logger: message
    LinePattern(_compile(rf"^{_TS}\s+{_LVL}\s+{_LOGGER}:\s*{_MSG}$")),
    # TS [LEVEL] message   /   TS LEVEL message
    LinePattern(_compile(rf"^{_TS}\s+\[?{_LVL}\]?\s+{_MSG}$")),
    # [TS] [LEVEL] message   /   [TS] LEVEL message
    LinePattern(_compile(rf"^\[{_TS}\]\s+\[?{_LVL}\]?\s*{_MSG}$")),
    # TS message   (timestamped, no level)
    LinePattern(_compile(rf"^{_TS}\s+{_MSG}$")),
    # [LEVEL] message   /   LEVEL message
    LinePattern(_compile(rf"^\[?{_LVL}\]?\s+{_MSG}$")),
)


# ---------------------------------------------------------------------------
# Fallbacks used to still salvage a leading timestamp / level when no full
# pattern matches, so structure is extracted opportunistically.
# ---------------------------------------------------------------------------

LEADING_TIMESTAMP = _compile(rf"^{_TS}\b")
LEADING_LEVEL = _compile(rf"^\[?{_LVL}\]?\b")

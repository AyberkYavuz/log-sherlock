"""Deterministic error filtering and fingerprinting — the pass before the LLM.

This module turns a full ``parsed_logs`` payload into a short, counted list of
:class:`~models.ErrorSignature` s. It is pure arithmetic and regex: no prompts,
no network, no model. Given the same input it always returns the same
signatures, in the same order, with the same ids.

It exists to make the LLM pass *possible*. A 700k-line log can hold tens of
thousands of error records that are really a handful of distinct failures
repeated; sending them raw would blow any context window and bury the signal.
Three steps fix that:

    1. **Filter** — keep only records at an error severity, with a documented
       fallback to warnings so a warning-only payload still gets analyzed
       (see :data:`ERROR_SEVERITIES` / :data:`WARNING_SEVERITIES`).
    2. **Collate & mask** — attach orphaned continuation lines (Python
       tracebacks, Java stack frames) to the error they belong to, then replace
       every variable token (ids, IPs, addresses, numbers) with a placeholder
       so two occurrences of the same failure produce byte-identical text.
    3. **Group & cap** — collapse identical templates into counted signatures,
       rank them by volume and hand the LLM at most
       :data:`MAX_SIGNATURES_FOR_LLM`, reporting anything dropped.

What this module deliberately does *not* do is interpret. Every field it fills
is a count, a timestamp or a masked string it derived mechanically;
``is_root_cause_candidate`` and ``explanation`` are left at their empty
defaults for the LLM pass to fill.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from models import ErrorSignature, ErrorSummary, ParsedLogEntry

#: Levels treated as hard errors. Broader than ``timeline.ERROR_LEVELS``
#: because this node is *about* errors and should catch every spelling a
#: framework might emit (``SEVERE`` from ``java.util.logging``, ``EMERGENCY``
#: from syslog, ``EXCEPTION`` from .NET-style loggers).
ERROR_SEVERITIES: frozenset[str] = frozenset(
    {"ERROR", "CRITICAL", "FATAL", "SEVERE", "EMERGENCY", "EXCEPTION"}
)

#: The fallback tier. A payload with zero hard errors is not necessarily a
#: healthy one — an order service that logs every failed checkout at ``WARN``
#: still has an incident worth explaining — so warnings are analyzed rather
#: than the node reporting "nothing found".
WARNING_SEVERITIES: frozenset[str] = frozenset({"WARN", "WARNING"})

#: How many signatures the LLM is shown. Ranked by volume, so the cap drops the
#: long tail of one-off errors rather than anything load-bearing. Chosen to keep
#: the batch prompt comfortably inside a small model's context.
MAX_SIGNATURES_FOR_LLM = 25

#: How many unmasked examples each signature carries into the prompt.
SAMPLE_MESSAGE_LIMIT = 2

#: Longest sample message kept, in characters. Generous enough for a full
#: Python traceback (which is the most useful thing a sample can be) while
#: still bounding a pathological single-line dump.
SAMPLE_MESSAGE_MAX_LENGTH = 1200

#: Longest collated message that is fingerprinted. Beyond this the tail cannot
#: change the template in any way a reader would care about, and masking cost
#: grows with length.
TEMPLATE_MAX_LENGTH = 2000

#: Emitted verbatim when no error- *or* warning-severity record exists.
NO_ERRORS_NOTE = (
    "Error analysis skipped: No error- or warning-level entries present in "
    "parsed logs."
)


# ---------------------------------------------------------------------------
# Traceback collation
# ---------------------------------------------------------------------------
# A parser sees one line at a time, so a Python traceback arrives as N separate
# entries with no level and no timestamp — the exception type and message, the
# single most diagnostic part of the record, ends up detached from the ERROR
# line that introduced it. These patterns recognize such orphans so they can be
# reattached before fingerprinting.

_CONTINUATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^Traceback \(most recent call last\)"),
    re.compile(r"^During handling of the above exception"),
    re.compile(r"^The above exception was the direct cause"),
    # Python frame lines, and the caret/tilde markers CPython 3.11+ emits.
    re.compile(r"^File\s+\"?.+\"?,\s+line\s+\d+"),
    re.compile(r"^[~^]+$"),
    # Java / JVM stack frames and their "Caused by:" chain.
    re.compile(r"^at\s+[\w$.]+\(.*\)"),
    re.compile(r"^Caused by:\s"),
    re.compile(r"^\.{3}\s+\d+\s+more$"),
    # A bare "SomeError: message" line, i.e. the terminal line of a traceback.
    re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning)(?::|$)"),
)

#: Opens a Python traceback. Once this is seen, the record continues until the
#: exception line closes it — see :func:`_collect`.
_TRACEBACK_START = re.compile(r"^Traceback \(most recent call last\)")

#: Closes a traceback: the ``SomeError: detail`` line CPython prints last.
_EXCEPTION_TERMINATOR = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception)(?::|$)")

#: A continuation line can only be attached to an error this many lines back.
#: Guards against a stray unlevelled line hundreds of lines later being glued
#: onto an unrelated error.
_MAX_COLLATION_GAP = 2

#: Hard ceiling on the lines one record may absorb. A traceback that never
#: prints its exception line (truncated output, an interleaved writer) would
#: otherwise swallow the rest of the file.
_MAX_CONTINUATION_LINES = 60


def _text_of(entry: ParsedLogEntry) -> str:
    """The entry's message, falling back to its raw line."""
    return (entry.get("message") or entry.get("raw") or "").strip()


def is_continuation(entry: ParsedLogEntry) -> bool:
    """Whether ``entry`` is an orphaned fragment of a multi-line error record.

    A continuation carries no level of its own — a line the parser *did* assign
    a level to is a record in its own right and is never swallowed, however
    traceback-shaped it looks.
    """
    if entry.get("level"):
        return False
    text = _text_of(entry)
    return any(pattern.match(text) for pattern in _CONTINUATION_PATTERNS)


def collate_message(entry: ParsedLogEntry, continuations: list[ParsedLogEntry]) -> str:
    """Join an error entry with its continuation lines into one message."""
    parts = [(entry.get("message") or entry.get("raw") or "").strip()]
    parts.extend(
        (line.get("message") or line.get("raw") or "").strip() for line in continuations
    )
    return "\n".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# Parameter masking
# ---------------------------------------------------------------------------
# Order is load-bearing and the list below is applied top to bottom. Every
# pattern is anchored on a word boundary or an unambiguous prefix so a
# placeholder can never be produced from the middle of an identifier.
#
# The general rule is *most specific first*: a UUID must be consumed before its
# digits can be eaten by the numeric rule, a long ``0x7f...`` address before the
# generic hex rule, and a full timestamp before its components. Each entry is a
# ``(compiled pattern, replacement)`` pair.

_MASKING_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # -- UUIDs / GUIDs, optionally brace-wrapped -----------------------------
    (
        re.compile(
            r"\{?\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b\}?"
        ),
        "<UUID>",
    ),
    # -- Timestamps, before the numeric rule can shred them ------------------
    # ISO-8601, with optional fractional seconds and zone.
    (
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
            r"(?:Z|[+-]\d{2}:?\d{2})?"
        ),
        "<NUM>",
    ),
    # A bare date, or a bare clock time.
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<NUM>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<NUM>"),
    # -- Network addresses ---------------------------------------------------
    # IPv6 first: its colons and hex groups would otherwise be partly eaten by
    # the IPv4 and numeric rules.
    #
    # The two lookarounds are what keep this rule honest. A ``::`` that is
    # merely a scope operator — ``std::vector``, ``App\Http::handle``,
    # ``Service::run`` — has a word character pressed against it, and rejecting
    # those is the difference between masking an address and corrupting every
    # C++, Rust, PHP and Ruby symbol in a stack trace. Each group must also
    # carry at least one hex digit, so ``::`` alone can never match.
    (
        re.compile(
            r"(?<![\w:.])"
            r"(?:"
            # Full form: eight groups.
            r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
            # Compressed form: leading groups, then ``::``, then optional trailing groups.
            r"|(?:[0-9a-fA-F]{1,4}:){1,7}:(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4})*)?"
            # Compressed form anchored at the start (``::1``).
            r"|::(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4})*)"
            r")"
            r"(?:/\d{1,3})?"
            r"(?![\w:.])"
        ),
        "<IP>",
    ),
    # IPv4, with an optional port. The port keeps its own placeholder because
    # "which port" is usually the interesting half of a connection failure.
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b:\d{1,5}\b"), "<IP>:<PORT>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b(?:/\d{1,2})?"), "<IP>"),
    # -- Memory addresses, before the generic hex rule -----------------------
    # A pointer-width ``0x`` literal (8+ hex digits) is an address; a short one
    # is far more likely an error code or a mask, and falls through to <HEX>.
    (re.compile(r"\b0[xX][0-9a-fA-F]{8,}\b"), "<ADDR>"),
    # -- Hex literals and hash digests ---------------------------------------
    (re.compile(r"\b0[xX][0-9a-fA-F]+\b"), "<HEX>"),
    # Bare md5 / sha1 / sha256 digests. Length-anchored so an ordinary word of
    # hex-ish letters (``deadbeef``) is not mistaken for one.
    (re.compile(r"\b[0-9a-fA-F]{64}\b"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{40}\b"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{32}\b"), "<HEX>"),
    # -- Numbers -------------------------------------------------------------
    # Trailing digits glued to an identifier by a separator: ORDER-5001,
    # PRODUCT_19, worker.3. Masking these is what collapses per-entity error
    # storms into one signature.
    (re.compile(r"(?<=[A-Za-z])([-_.:#])\d+\b"), r"\1<NUM>"),
    # A measurement with a unit glued to it (``30.5s``, ``512MB``, ``1200ms``).
    # The number is the variable part and the unit is not, so only the number
    # is replaced — this must run before the general rule below, whose
    # "not followed by a word character" guard would otherwise skip it.
    (
        re.compile(
            r"(?<![\w.])[-+]?\d+(?:\.\d+)?"
            r"(?=(?:ns|us|ms|s|m|h|d|kb|mb|gb|tb|kib|mib|gib|b)\b)",
            re.IGNORECASE,
        ),
        "<NUM>",
    ),
    # A number using thousands separators (``1,234,567``), matched as one token
    # so the separators are not mistaken for punctuation between numbers.
    (re.compile(r"(?<![\w.])[-+]?\d{1,3}(?:,\d{3})+(?![\w.])"), "<NUM>"),
    # Any remaining standalone number: ints, decimals, scientific notation,
    # optional sign. Bounded on both sides so a number embedded in an
    # identifier (``utf8``, ``sha256``, ``v1``) is left intact — that is
    # identity, not a parameter.
    (
        re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.])"),
        "<NUM>",
    ),
)


def mask_message(message: str) -> str:
    """Replace every variable token in ``message`` with a stable placeholder.

    This is the whole basis of grouping: two occurrences of the same failure
    differ only in their variable parts, so masking those away makes the
    remaining text an identity for the failure class.

    Args:
        message: The raw (collated) message text.

    Returns:
        The masked template, with runs of whitespace — including the newlines
        introduced by traceback collation — normalized to single spaces so
        formatting differences cannot split a group.
    """
    masked = message[:TEMPLATE_MAX_LENGTH]
    for pattern, replacement in _MASKING_RULES:
        masked = pattern.sub(replacement, masked)
    return " ".join(masked.split())


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _level_of(entry: ParsedLogEntry) -> str:
    """The entry's normalized level, or ``""`` when it has none."""
    level = entry.get("level")
    return level.strip().upper() if isinstance(level, str) else ""


def select_error_entries(
    parsed_logs: list[ParsedLogEntry],
) -> tuple[list[tuple[ParsedLogEntry, str]], bool]:
    """Pick the records to analyze, pairing each with its collated message.

    Hard errors win outright. Only when there are none does the warning tier
    apply — the two are never mixed, because a payload containing three real
    ``ERROR`` s should not have its analysis diluted by nine hundred warnings.

    Args:
        parsed_logs: Normalized entries from the parser node, in file order.

    Returns:
        A ``(selected, used_warning_fallback)`` pair. ``selected`` holds
        ``(entry, collated_message)`` tuples in file order;
        ``used_warning_fallback`` records which tier produced them, so the
        caller can say so in its notes.
    """
    hard = _collect(parsed_logs, ERROR_SEVERITIES)
    if hard:
        return hard, False
    return _collect(parsed_logs, WARNING_SEVERITIES), True


def _collect(
    parsed_logs: list[ParsedLogEntry], levels: frozenset[str]
) -> list[tuple[ParsedLogEntry, str]]:
    """Collect entries at ``levels``, absorbing each one's continuation lines.

    A single forward pass: on matching an entry, walk forward over the
    immediately following continuation lines and fold them into its message.
    Continuations are consumed, so a traceback never becomes a record of its
    own.

    The walk runs in two modes. Normally it absorbs only lines that *look* like
    continuations (:data:`_CONTINUATION_PATTERNS`). But once a
    ``Traceback (most recent call last):`` line opens a Python traceback, it
    switches to absorbing **every** adjacent unlevelled line until the
    exception line closes the record — because the body of a traceback contains
    arbitrary source code (``return inference_service.predict(``) that no
    pattern can be expected to recognize. A levelled line always ends the
    record: that is the next log entry, not part of this one.
    """
    selected: list[tuple[ParsedLogEntry, str]] = []
    index = 0
    total = len(parsed_logs)

    while index < total:
        entry = parsed_logs[index]
        index += 1
        if _level_of(entry) not in levels:
            continue

        continuations: list[ParsedLogEntry] = []
        cursor = index
        previous_line = entry.get("line_number")
        in_traceback = False

        while cursor < total and len(continuations) < _MAX_CONTINUATION_LINES:
            candidate = parsed_logs[cursor]

            # A line the parser assigned a level to is the next record.
            if candidate.get("level"):
                break

            # Only absorb lines that are actually adjacent in the source; a gap
            # means the parser skipped something in between and the two are not
            # one record.
            line = candidate.get("line_number")
            if (
                isinstance(line, int)
                and isinstance(previous_line, int)
                and line - previous_line > _MAX_COLLATION_GAP
            ):
                break

            if not in_traceback and not is_continuation(candidate):
                break

            continuations.append(candidate)
            previous_line = line
            cursor += 1

            text = _text_of(candidate)
            if in_traceback:
                if _EXCEPTION_TERMINATOR.match(text):
                    break
            elif _TRACEBACK_START.match(text):
                in_traceback = True

        index = cursor
        selected.append((entry, collate_message(entry, continuations)))

    return selected


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


class _Group:
    """Mutable accumulator for one template while the payload is scanned.

    Kept private: it exists only to build an :class:`~models.ErrorSignature`,
    which is the type that actually crosses a node boundary.
    """

    __slots__ = (
        "template",
        "severity",
        "count",
        "loggers",
        "samples",
        "first_ts",
        "last_ts",
        "first_line",
        "last_line",
        "order",
    )

    def __init__(self, template: str, severity: str, order: int) -> None:
        self.template = template
        self.severity = severity
        self.order = order  # first-appearance index, used as a stable tiebreak
        self.count = 0
        self.loggers: set[str] = set()
        self.samples: list[str] = []
        self.first_ts: datetime | None = None
        self.last_ts: datetime | None = None
        self.first_line: int | None = None
        self.last_line: int | None = None

    def add(self, entry: ParsedLogEntry, message: str) -> None:
        """Fold one occurrence into the group."""
        self.count += 1

        logger = entry.get("logger")
        if isinstance(logger, str) and logger.strip():
            self.loggers.add(logger.strip())

        if len(self.samples) < SAMPLE_MESSAGE_LIMIT:
            self.samples.append(_truncate(message))

        timestamp = entry.get("timestamp")
        if isinstance(timestamp, datetime):
            if self.first_ts is None or _comparable(timestamp) < _comparable(
                self.first_ts
            ):
                self.first_ts = timestamp
            if self.last_ts is None or _comparable(timestamp) > _comparable(
                self.last_ts
            ):
                self.last_ts = timestamp

        line = entry.get("line_number")
        if isinstance(line, int):
            if self.first_line is None or line < self.first_line:
                self.first_line = line
            if self.last_line is None or line > self.last_line:
                self.last_line = line

    def to_signature(self, signature_id: str) -> ErrorSignature:
        """Freeze the accumulator into the shared model.

        ``is_root_cause_candidate`` and ``explanation`` are seeded with their
        empty defaults — this pass has no basis for either, and inventing one
        would be exactly the interpretation it is meant to avoid.
        """
        return {
            "signature_id": signature_id,
            "template": self.template,
            "severity": self.severity,
            "count": self.count,
            "first_seen": self._boundary(self.first_ts, self.first_line),
            "last_seen": self._boundary(self.last_ts, self.last_line),
            "loggers": sorted(self.loggers),
            "sample_messages": list(self.samples),
            "is_root_cause_candidate": False,
            "explanation": "",
        }

    def _boundary(self, moment: datetime | None, line: int | None) -> str | None:
        """Render a first/last marker, preferring time over position.

        Timestamps are emitted exactly as the parser normalized them; this
        module never re-parses or repairs one. Entries the parser could not
        stamp fall back to their line number, which keeps the marker traceable
        instead of ``None``.
        """
        if moment is not None:
            return moment.isoformat()
        if line is not None:
            return f"line {line}"
        return None


def _comparable(moment: datetime) -> datetime:
    """Make naive and aware timestamps orderable against each other.

    A payload can mix both (a zoned application log and a bare syslog line).
    Naive stamps are read as UTC — the same convention the timeline node uses —
    so a comparison never raises.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _truncate(message: str) -> str:
    """Bound a sample message, marking the cut so it cannot be misread."""
    if len(message) <= SAMPLE_MESSAGE_MAX_LENGTH:
        return message
    return message[: SAMPLE_MESSAGE_MAX_LENGTH - 3] + "..."


def build_error_summary(
    parsed_logs: list[ParsedLogEntry],
) -> tuple[ErrorSummary, list[str]]:
    """Filter, fingerprint and group ``parsed_logs`` into an ``ErrorSummary``.

    This is the module's entry point and the whole deterministic pass. The
    summary it returns is already valid and complete except for the two LLM
    fields (``primary_error_signature_id`` stays ``None`` and
    ``cascading_impact_summary`` stays ``""``); the node fills those in and
    otherwise passes this straight through.

    Args:
        parsed_logs: Normalized entries from the parser node, in file order.

    Returns:
        A ``(summary, notes)`` pair. ``notes`` explains what was analyzed and
        what was left out — the warning fallback, the prompt cap — so nothing
        the caller would want to know is dropped silently.
    """
    selected, used_fallback = select_error_entries(parsed_logs)
    if not selected:
        return _empty_summary(), [NO_ERRORS_NOTE]

    groups: dict[tuple[str, str], _Group] = {}
    for entry, message in selected:
        template = mask_message(message)
        severity = _level_of(entry)
        # Severity is part of the key, so a CRITICAL and an ERROR that mask to
        # the same text stay distinguishable rather than being averaged into
        # one misleading signature.
        key = (template, severity)
        group = groups.get(key)
        if group is None:
            group = _Group(template, severity, len(groups))
            groups[key] = group
        group.add(entry, message)

    # Loudest first. First-appearance order breaks ties so equal-count
    # signatures keep a stable, source-ordered arrangement across runs.
    ranked = sorted(groups.values(), key=lambda g: (-g.count, g.order))
    kept = ranked[:MAX_SIGNATURES_FOR_LLM]

    signatures = [
        group.to_signature(f"ERR_{position:03d}")
        for position, group in enumerate(kept, start=1)
    ]

    summary: ErrorSummary = {
        "total_errors_analyzed": len(selected),
        "unique_signatures_found": len(groups),
        "primary_error_signature_id": None,
        "signatures": signatures,
        "cascading_impact_summary": "",
    }
    return summary, _build_notes(
        selected_count=len(selected),
        group_count=len(groups),
        kept_count=len(kept),
        omitted=ranked[MAX_SIGNATURES_FOR_LLM:],
        used_fallback=used_fallback,
    )


def _build_notes(
    *,
    selected_count: int,
    group_count: int,
    kept_count: int,
    omitted: list[_Group],
    used_fallback: bool,
) -> list[str]:
    """State what was analyzed and, above all, what was not."""
    tier = "warning-level" if used_fallback else "error-level"
    notes = [
        f"Error analysis: fingerprinted {selected_count} {tier} "
        f"{_plural(selected_count, 'entry', 'entries')} into {group_count} "
        f"unique {_plural(group_count, 'signature', 'signatures')}."
    ]

    if used_fallback:
        notes.append(
            "Error analysis: no ERROR/CRITICAL/FATAL entries were present, so "
            "WARN/WARNING entries were analyzed instead."
        )

    if omitted:
        dropped_occurrences = sum(group.count for group in omitted)
        notes.append(
            f"Error analysis: {len(omitted)} of {group_count} signatures "
            f"({dropped_occurrences} "
            f"{_plural(dropped_occurrences, 'occurrence', 'occurrences')}) were "
            f"omitted from LLM analysis; only the top {kept_count} by volume "
            "were submitted."
        )

    return notes


def _plural(count: int, singular: str, plural: str) -> str:
    """Return ``singular`` when ``count == 1`` else ``plural``."""
    return singular if count == 1 else plural


def _empty_summary() -> ErrorSummary:
    """The summary for a payload with nothing to analyze."""
    return {
        "total_errors_analyzed": 0,
        "unique_signatures_found": 0,
        "primary_error_signature_id": None,
        "signatures": [],
        "cascading_impact_summary": "",
    }

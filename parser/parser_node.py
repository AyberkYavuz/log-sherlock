"""The deterministic Parser Node for the LogSherlock graph.

This node converts ``raw_logs`` into normalized ``parsed_logs`` — the single
source of truth every downstream node consumes. It is fully deterministic: no
LLMs, no prompts, no network. Given the same ``raw_logs`` it always returns the
same result.

The public entry point is :func:`parser_node`, whose signature matches the stub
in ``graph.py`` (full state in, partial state delta out). ``graph.py`` is frozen,
so this module is imported/wired by the graph owner rather than editing the
graph itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base_parser import BaseParser
from .models import LogFormat, ParsedLogEntry
from .parser_factory import select_parser


@dataclass
class _ParseOutcome:
    """Accumulated results of parsing every line of a log payload."""

    entries: list[ParsedLogEntry] = field(default_factory=list)
    malformed_count: int = 0
    missing_timestamp_count: int = 0


def _split_lines(raw_logs: str) -> list[str]:
    """Split raw log text into lines, dropping the universal-newline endings.

    ``str.splitlines`` handles ``\\n``, ``\\r\\n`` and ``\\r`` uniformly, which
    keeps line numbering stable across platforms.
    """
    return raw_logs.splitlines()


def _parse_lines(lines: list[str], parser: BaseParser) -> _ParseOutcome:
    """Parse each line with ``parser``, tolerating malformed input.

    Blank / whitespace-only lines are skipped silently (they are not data and
    are not counted as malformed). ``line_number`` is the 1-based index within
    the original text so entries stay traceable back to the source.
    """
    outcome = _ParseOutcome()
    for index, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        entry = parser.parse_line(index, raw)
        if entry is None:
            outcome.malformed_count += 1
            continue
        outcome.entries.append(entry)
        if entry.timestamp is None:
            outcome.missing_timestamp_count += 1
    return outcome


def _build_notes(
    log_format: LogFormat, entry_count: int, outcome: _ParseOutcome
) -> list[str]:
    """Compose the human-readable ``investigation_notes`` for this run."""
    notes = [f"Parser: detected log format '{log_format.value}'."]
    notes.append(f"Parser: parsed {entry_count} log entr" + ("y." if entry_count == 1 else "ies."))
    if outcome.malformed_count:
        notes.append(
            f"Parser: skipped {outcome.malformed_count} malformed line"
            f"{'' if outcome.malformed_count == 1 else 's'}."
        )
    if outcome.missing_timestamp_count:
        notes.append(
            f"Parser: {outcome.missing_timestamp_count} entr"
            f"{'y is' if outcome.missing_timestamp_count == 1 else 'ies are'} "
            "missing a timestamp."
        )
    return notes


def parser_node(state: dict[str, Any]) -> dict[str, Any]:
    """Parse ``raw_logs`` into normalized ``parsed_logs``.

    Args:
        state: The LogSherlock graph state. Only ``raw_logs`` is read.

    Returns:
        A partial state delta containing exactly:

            * ``parsed_logs`` — list of normalized entry dicts,
            * ``investigation_notes`` — notes about format, counts and gaps,
            * ``completed_stages`` — ``["parser"]``.

        No other state fields are touched. ``investigation_notes`` and
        ``completed_stages`` use additive reducers in the graph, so returning
        only this node's contributions is correct.
    """
    raw_logs = state.get("raw_logs") or ""
    lines = _split_lines(raw_logs)

    if not any(line.strip() for line in lines):
        return {
            "parsed_logs": [],
            "investigation_notes": ["Parser: no log lines to parse (empty input)."],
            "completed_stages": ["parser"],
        }

    parser = select_parser(lines)
    outcome = _parse_lines(lines, parser)
    parsed_logs = [entry.to_dict() for entry in outcome.entries]
    notes = _build_notes(parser.log_format, len(parsed_logs), outcome)

    return {
        "parsed_logs": parsed_logs,
        "investigation_notes": notes,
        "completed_stages": ["parser"],
    }

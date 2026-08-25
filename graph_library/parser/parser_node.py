"""The deterministic Parser Node for the LogSherlock graph.

This node converts ``raw_logs`` into normalized ``parsed_logs`` — the single
source of truth every downstream node consumes. It is fully deterministic: no
LLMs, no prompts, no network. Given the same ``raw_logs`` it always returns the
same result.

Alongside ``parsed_logs`` it emits:

    * ``parser_metrics`` — structured, machine-readable run health
      (:class:`~graph_library.models.parser_metrics.ParserMetrics`) for downstream nodes,
    * ``investigation_notes`` — the same facts phrased for humans.

The public entry point is :func:`parser_node`, whose signature matches the stub
in ``graph.py`` (full state in, partial state delta out).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from graph_library.models import ParsedLogEntry, ParserMetrics

from .base_parser import BaseParser
from .parser_factory import detect


@dataclass
class _ParseOutcome:
    """Accumulated results of parsing every line of a log payload."""

    entries: list[ParsedLogEntry] = field(default_factory=list)
    blank_lines: int = 0
    malformed_lines: int = 0
    missing_timestamp_lines: int = 0


def _split_lines(raw_logs: str) -> list[str]:
    """Split raw log text into lines, dropping the line endings.

    ``str.splitlines`` handles ``\\n``, ``\\r\\n`` and ``\\r`` uniformly, which
    keeps line numbering stable across platforms.
    """
    return raw_logs.splitlines()


def _parse_lines(lines: list[str], parser: BaseParser) -> _ParseOutcome:
    """Parse each line with ``parser``, tolerating malformed input.

    Blank / whitespace-only lines are skipped silently (they are not data and
    are not malformed). ``line_number`` is the 1-based index within the original
    text so entries stay traceable back to the source.
    """
    outcome = _ParseOutcome()
    for index, raw in enumerate(lines, start=1):
        if not raw.strip():
            outcome.blank_lines += 1
            continue
        entry = parser.parse_line(index, raw)
        if entry is None:
            outcome.malformed_lines += 1
            continue
        outcome.entries.append(entry)
        if entry["timestamp"] is None:
            outcome.missing_timestamp_lines += 1
    return outcome


def _build_metrics(
    parser: BaseParser,
    confidence: float,
    total_lines: int,
    outcome: _ParseOutcome,
) -> ParserMetrics:
    """Assemble the structured :class:`ParserMetrics` for this run."""
    return ParserMetrics(
        parser_name=type(parser).__name__,
        parser_confidence=confidence,
        detected_format=parser.log_format.value,
        total_lines=total_lines,
        blank_lines=outcome.blank_lines,
        parsed_lines=len(outcome.entries),
        malformed_lines=outcome.malformed_lines,
        missing_timestamp_lines=outcome.missing_timestamp_lines,
    )


def _build_notes(metrics: ParserMetrics) -> list[str]:
    """Compose human-readable ``investigation_notes`` from the metrics."""
    if metrics["parsed_lines"] == 0 and metrics["malformed_lines"] == 0:
        return ["Parser: no log lines to parse (empty input)."]

    notes = [
        f"Parser: detected log format '{metrics['detected_format']}' "
        f"using {metrics['parser_name']} "
        f"(confidence {metrics['parser_confidence']:.2f}).",
        f"Parser: parsed {metrics['parsed_lines']} "
        + _plural(metrics["parsed_lines"], "log entry", "log entries")
        + ".",
    ]
    if metrics["malformed_lines"]:
        notes.append(
            f"Parser: skipped {metrics['malformed_lines']} malformed "
            + _plural(metrics["malformed_lines"], "line", "lines")
            + "."
        )
    if metrics["missing_timestamp_lines"]:
        count = metrics["missing_timestamp_lines"]
        notes.append(
            f"Parser: {count} "
            + _plural(count, "entry is", "entries are")
            + " missing a timestamp."
        )
    return notes


def _plural(count: int, singular: str, plural: str) -> str:
    """Return ``singular`` when ``count == 1`` else ``plural``."""
    return singular if count == 1 else plural


def parser_node(state: dict[str, Any]) -> dict[str, Any]:
    """Parse ``raw_logs`` into normalized ``parsed_logs`` and metrics.

    Args:
        state: The LogSherlock graph state. Only ``raw_logs`` is read.

    Returns:
        A partial state delta containing exactly:

            * ``parsed_logs`` — list of :class:`ParsedLogEntry` dicts,
            * ``parser_metrics`` — structured run health for downstream nodes,
            * ``investigation_notes`` — human-readable notes,
            * ``completed_stages`` — ``["parser"]``.

        No other state fields are touched. ``investigation_notes`` and
        ``completed_stages`` use additive reducers in the graph, so returning
        only this node's contributions is correct.
    """
    raw_logs = state.get("raw_logs") or ""
    lines = _split_lines(raw_logs)

    detection = detect(lines)
    outcome = _parse_lines(lines, detection.parser)
    metrics = _build_metrics(
        detection.parser, detection.confidence, len(lines), outcome
    )

    return {
        "parsed_logs": outcome.entries,
        "parser_metrics": metrics,
        "investigation_notes": _build_notes(metrics),
        "completed_stages": ["parser"],
    }

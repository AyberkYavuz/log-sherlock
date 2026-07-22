"""Structured, machine-readable health metrics emitted by the parser.

These numbers used to live only inside human-readable ``investigation_notes``.
Promoting them to a typed graph field lets downstream nodes consume parser
health deterministically — e.g. the Statistics Node reads ``parsed_lines`` and
``malformed_lines`` here instead of recomputing them from ``raw_logs``.

``investigation_notes`` stay for humans; ``ParserMetrics`` is for machines.
"""

from __future__ import annotations

from typing import TypedDict


class ParserMetrics(TypedDict):
    """A deterministic summary of a single parser run.

    Invariant:
        ``total_lines == blank_lines + parsed_lines + malformed_lines``.

    Attributes:
        parser_name: Class name of the parser that was selected
            (e.g. ``"JSONLinesParser"``).
        parser_confidence: Detection confidence of the selected parser on the
            sampled input, in ``[0.0, 1.0]``.
        detected_format: The chosen :class:`~models.log_format.LogFormat` value
            (e.g. ``"json"``).
        total_lines: Total number of lines in ``raw_logs`` (blanks included).
        blank_lines: Lines that were empty / whitespace-only and skipped.
        parsed_lines: Lines successfully turned into entries.
        malformed_lines: Non-blank lines the parser could not parse (skipped).
        missing_timestamp_lines: Parsed entries whose ``timestamp`` is ``None``.
    """

    parser_name: str
    parser_confidence: float
    detected_format: str
    total_lines: int
    blank_lines: int
    parsed_lines: int
    malformed_lines: int
    missing_timestamp_lines: int

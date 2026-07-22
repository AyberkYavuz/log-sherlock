"""Format detection and parser selection.

The factory owns the registry of available parsers and the deterministic rule
for choosing one. To support a new format, add its parser to
:data:`PARSER_REGISTRY` — detection and node wiring pick it up automatically.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from .base_parser import BaseParser
from .json_parser import JSONLinesParser
from .text_parser import PlainTextParser

# Number of leading non-empty lines inspected during format detection. Sampling
# keeps detection O(1) in the size of the log payload while staying stable.
_SAMPLE_SIZE = 50

# The universal fallback parser, used when no structured parser scores higher.
# Plain text can represent anything, so it is never "wrong", only least specific.
_FALLBACK_PARSER: BaseParser = PlainTextParser()

# Structured parsers, tried against the fallback during detection. Add a new
# structured format here to make it discoverable.
_STRUCTURED_PARSERS: tuple[BaseParser, ...] = (JSONLinesParser(),)

# All parsers known to the pipeline (fallback last).
PARSER_REGISTRY: tuple[BaseParser, ...] = (*_STRUCTURED_PARSERS, _FALLBACK_PARSER)


def sample_lines(lines: Sequence[str], limit: int = _SAMPLE_SIZE) -> list[str]:
    """Return up to ``limit`` non-blank, stripped lines for detection."""
    sampled: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped:
            sampled.append(stripped)
        if len(sampled) >= limit:
            break
    return sampled


class Detection(NamedTuple):
    """Result of format detection: the chosen parser and its confidence."""

    parser: BaseParser
    confidence: float


def detect(lines: Sequence[str]) -> Detection:
    """Detect the best parser for ``lines`` and report its confidence.

    Deterministic: each structured parser is scored against a fixed sample and
    only replaces the fallback when it scores *strictly higher*. As a result the
    plain-text fallback wins every tie (including empty input), and detection is
    stable for the same input. Always returns a parser — never ``None``.
    """
    sample = sample_lines(lines)
    best = _FALLBACK_PARSER
    best_score = _FALLBACK_PARSER.confidence(sample)
    for parser in _STRUCTURED_PARSERS:
        score = parser.confidence(sample)
        if score > best_score:
            best, best_score = parser, score
    return Detection(best, best_score)


def select_parser(lines: Sequence[str]) -> BaseParser:
    """Return just the best parser for ``lines`` (see :func:`detect`)."""
    return detect(lines).parser

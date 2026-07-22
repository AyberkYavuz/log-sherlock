"""Abstract base class every concrete log parser implements.

The contract is intentionally tiny so adding a new format is cheap:

    1. Subclass :class:`BaseParser`.
    2. Implement :meth:`confidence` (how sure am I this is my format?) and
       :meth:`parse_line` (turn one line into a :class:`ParsedLogEntry`).
    3. Register the subclass in ``parser_factory.PARSER_REGISTRY``.

Nothing else in the pipeline needs to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from .models import LogFormat, ParsedLogEntry


class BaseParser(ABC):
    """Interface shared by all format-specific parsers.

    Concrete parsers are stateless and cheap to instantiate; the factory keeps
    a single instance per format.
    """

    #: The format this parser produces. Set by each subclass.
    log_format: LogFormat

    @abstractmethod
    def confidence(self, sample_lines: Sequence[str]) -> float:
        """Return how strongly ``sample_lines`` look like this parser's format.

        The value is a score in ``[0.0, 1.0]``; the factory selects the parser
        with the highest score. Implementations must be side-effect free and
        deterministic — the same input always yields the same score.

        Args:
            sample_lines: A sample of non-empty log lines (already stripped of
                surrounding whitespace).

        Returns:
            A confidence score between 0.0 (definitely not this format) and 1.0.
        """

    @abstractmethod
    def parse_line(self, line_number: int, raw: str) -> ParsedLogEntry | None:
        """Parse a single line into a normalized entry.

        Args:
            line_number: 1-based position of the line in the source text.
            raw: The original line (without its trailing newline).

        Returns:
            A :class:`ParsedLogEntry`, or ``None`` if the line is malformed for
            this format and should be skipped. Implementations must never raise
            on malformed input — return ``None`` instead so one bad line cannot
            halt the investigation.
        """

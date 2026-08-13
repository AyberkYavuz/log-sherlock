"""Shared graph models — the single source of truth for LogSherlock.

Every data structure that crosses a node boundary is defined here, exactly
once, and imported by the feature packages (``parser``, ``statistics``,
``timeline``, ``recommendation``, ``report``, ``api``, ``database``).

Architectural rules:
    * Shared models live ONLY in this package.
    * Feature packages import models from here; they never redefine them.
    * This package contains no business logic and MUST NOT import any feature
      package — the dependency arrow always points *toward* models.

Future shared models (``ErrorSummary``, ``PatternSummary``, ``TimelineEvent``,
``HistoricalInvestigation``, ``StructuredReport``, ``ExecutionMetadata``, ...)
will be added here as their nodes are implemented, each in its own module and
re-exported below.
"""

from __future__ import annotations

from .log_format import LogFormat
from .parsed_log import ParsedLogEntry
from .parser_metrics import ParserMetrics
from .statistics import (
    CategoryCount,
    SeveritySummary,
    Statistics,
    TimestampCoverage,
)

__all__ = [
    "LogFormat",
    "ParsedLogEntry",
    "ParserMetrics",
    "CategoryCount",
    "SeveritySummary",
    "Statistics",
    "TimestampCoverage",
]

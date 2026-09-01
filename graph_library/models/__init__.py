"""Shared graph models — the single source of truth for LogSherlock.

Every data structure that crosses a node boundary is defined here, exactly
once, and imported by the feature packages under ``graph_library/`` (``parser``,
``statistics``, ``timeline``, ``prepare_output``, ``write_to_db``, ``api``,
``database``).

Architectural rules:
    * Shared models live ONLY in this package.
    * Feature packages import models from here; they never redefine them.
    * This package contains no business logic and MUST NOT import any feature
      package — the dependency arrow always points *toward* models.

Future shared models (``HistoricalInvestigation``, ``StructuredReport``,
``ExecutionMetadata``, ...) will be added here as their nodes are implemented,
each in its own module and re-exported below.
"""

from __future__ import annotations

from .error_analysis import (
    MAX_SEARCH_QUERIES,
    AnalysisMode,
    ErrorSignature,
    ErrorSummary,
    LLMErrorAnalysisResult,
    LLMErrorSignatureEvaluation,
    LLMProvider,
    LLMSearchDecision,
)
from .log_format import LogFormat
from .parsed_log import ParsedLogEntry
from .parser_metrics import ParserMetrics
from .pattern_analysis import (
    AnomalyCategory,
    AnomalySeverity,
    PatternAnalysisResult,
    PatternSummary,
    SystemAnomaly,
    SystemAnomalyRecord,
)
from .statistics import (
    CategoryCount,
    SeveritySummary,
    Statistics,
    TimestampCoverage,
)
from .timeline import MilestoneKind, TimelineEvent, TimelineEventType

__all__ = [
    "AnalysisMode",
    "ErrorSignature",
    "ErrorSummary",
    "LLMErrorAnalysisResult",
    "LLMErrorSignatureEvaluation",
    "LLMProvider",
    "LLMSearchDecision",
    "MAX_SEARCH_QUERIES",
    "LogFormat",
    "ParsedLogEntry",
    "ParserMetrics",
    "AnomalyCategory",
    "AnomalySeverity",
    "PatternAnalysisResult",
    "PatternSummary",
    "SystemAnomaly",
    "SystemAnomalyRecord",
    "CategoryCount",
    "SeveritySummary",
    "Statistics",
    "TimestampCoverage",
    "MilestoneKind",
    "TimelineEvent",
    "TimelineEventType",
]

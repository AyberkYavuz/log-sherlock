"""Architectural guardrails for the shared ``graph_library.models`` package.

These tests encode the project's model-ownership rules so future refactors
cannot silently reintroduce duplicate definitions or model drift:

    * every shared model has exactly ONE class definition, and it lives in
      ``graph_library/models/``,
    * feature packages and the graph import those models from
      ``graph_library.models`` rather
      than redefining them,
    * the models are ``TypedDict`` s (dict-native graph state).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import graph
from graph_library import models
import graph_library.parser.json_parser as json_parser
import graph_library.parser.text_parser as text_parser
import graph_library.stats.aggregations as aggregations
import graph_library.timeline.buckets as timeline_buckets
from graph_library.models import (
    LogFormat,
    ParsedLogEntry,
    ParserMetrics,
    Statistics,
    TimelineEvent,
)

# Resolved via importlib because the ``graph_library.parser`` /
# ``graph_library.stats`` packages re-export a
# ``parser_node`` / ``statistics_node`` *function*, which shadows the
# same-named submodule attribute; ``import_module`` returns the module object
# regardless.
parser_node_module = importlib.import_module("graph_library.parser.parser_node")
statistics_node_module = importlib.import_module("graph_library.stats.statistics_node")
timeline_node_module = importlib.import_module("graph_library.timeline.node")
error_analysis_node_module = importlib.import_module("graph_library.error_analysis.node")
error_analysis_fingerprint = importlib.import_module("graph_library.error_analysis.fingerprint")

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _python_sources() -> list[Path]:
    """All first-party Python files, excluding caches and virtualenvs."""
    return [
        path
        for path in _REPO_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and ".venv" not in path.parts
    ]


def _count_class_definitions(class_name: str) -> list[Path]:
    """Return every source file that defines ``class <class_name>``."""
    pattern = re.compile(rf"^\s*class\s+{re.escape(class_name)}\b", re.MULTILINE)
    return [p for p in _python_sources() if pattern.search(p.read_text())]


# -- single source of truth -------------------------------------------------


def test_exactly_one_parsed_log_entry_definition() -> None:
    files = _count_class_definitions("ParsedLogEntry")
    assert len(files) == 1, f"expected 1 ParsedLogEntry definition, found {files}"
    assert files[0] == _REPO_ROOT / "graph_library" / "models" / "parsed_log.py"


def test_exactly_one_parser_metrics_definition() -> None:
    files = _count_class_definitions("ParserMetrics")
    assert len(files) == 1, f"expected 1 ParserMetrics definition, found {files}"
    assert files[0] == _REPO_ROOT / "graph_library" / "models" / "parser_metrics.py"


def test_exactly_one_statistics_definition() -> None:
    for name in ("Statistics", "CategoryCount", "SeveritySummary", "TimestampCoverage"):
        files = _count_class_definitions(name)
        assert len(files) == 1, f"expected 1 {name} definition, found {files}"
        assert files[0] == _REPO_ROOT / "graph_library" / "models" / "statistics.py"


def test_exactly_one_error_analysis_definition() -> None:
    for name in (
        "ErrorSignature",
        "ErrorSummary",
        "LLMErrorSignatureEvaluation",
        "LLMErrorAnalysisResult",
        "LLMSearchDecision",
    ):
        files = _count_class_definitions(name)
        assert len(files) == 1, f"expected 1 {name} definition, found {files}"
        assert files[0] == _REPO_ROOT / "graph_library" / "models" / "error_analysis.py"


def test_exactly_one_timeline_event_definition() -> None:
    files = _count_class_definitions("TimelineEvent")
    assert len(files) == 1, f"expected 1 TimelineEvent definition, found {files}"
    assert files[0] == _REPO_ROOT / "graph_library" / "models" / "timeline.py"


# -- shared imports (identity, not duplication) -----------------------------


def test_parser_imports_shared_models() -> None:
    # The names bound inside the parser modules must be the very objects
    # exported by ``graph_library.models`` — proving import, not redefinition.
    assert json_parser.ParsedLogEntry is ParsedLogEntry
    assert text_parser.ParsedLogEntry is ParsedLogEntry
    assert parser_node_module.ParsedLogEntry is ParsedLogEntry
    assert parser_node_module.ParserMetrics is ParserMetrics


def test_stats_imports_shared_models() -> None:
    assert statistics_node_module.Statistics is Statistics
    assert statistics_node_module.ParsedLogEntry is ParsedLogEntry
    assert aggregations.CategoryCount is models.CategoryCount


def test_timeline_imports_shared_models() -> None:
    assert timeline_node_module.TimelineEvent is TimelineEvent
    assert timeline_node_module.ParsedLogEntry is ParsedLogEntry
    assert timeline_buckets.TimelineEvent is TimelineEvent
    assert timeline_buckets.ParsedLogEntry is ParsedLogEntry


def test_error_analysis_imports_shared_models() -> None:
    assert error_analysis_fingerprint.ErrorSignature is models.ErrorSignature
    assert error_analysis_fingerprint.ErrorSummary is models.ErrorSummary
    assert error_analysis_fingerprint.ParsedLogEntry is ParsedLogEntry
    assert error_analysis_node_module.ErrorSummary is models.ErrorSummary
    assert (
        error_analysis_node_module.LLMErrorAnalysisResult
        is models.LLMErrorAnalysisResult
    )


def test_graph_imports_shared_models() -> None:
    assert graph.ParsedLogEntry is ParsedLogEntry
    assert graph.ParserMetrics is ParserMetrics
    assert graph.Statistics is Statistics
    assert graph.TimelineEvent is TimelineEvent


def test_models_package_exports() -> None:
    assert set(models.__all__) == {
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
        "CategoryCount",
        "SeveritySummary",
        "Statistics",
        "TimestampCoverage",
        "MilestoneKind",
        "TimelineEvent",
        "TimelineEventType",
    }


# -- TypedDict shape --------------------------------------------------------


def test_parsed_log_entry_is_typeddict() -> None:
    # TypedDict subclasses carry these marker attributes.
    assert hasattr(ParsedLogEntry, "__required_keys__")
    assert set(ParsedLogEntry.__annotations__) == {
        "line_number",
        "raw",
        "timestamp",
        "level",
        "logger",
        "message",
        "metadata",
    }


def test_statistics_is_typeddict() -> None:
    assert hasattr(Statistics, "__required_keys__")
    assert set(Statistics.__annotations__) == {
        "level_distribution",
        "logger_distribution",
        "severity",
        "timestamp_coverage",
        "metadata_distributions",
    }


def test_parser_metrics_is_typeddict() -> None:
    assert hasattr(ParserMetrics, "__required_keys__")
    assert set(ParserMetrics.__annotations__) == {
        "parser_name",
        "parser_confidence",
        "detected_format",
        "total_lines",
        "blank_lines",
        "parsed_lines",
        "malformed_lines",
        "missing_timestamp_lines",
    }


def test_timeline_event_is_typeddict() -> None:
    assert hasattr(TimelineEvent, "__required_keys__")
    assert set(TimelineEvent.__annotations__) == {
        "event_type",
        "timestamp",
        "end_timestamp",
        "milestone_kind",
        "total_logs",
        "error_count",
        "warning_count",
        "top_loggers",
        "sample_messages",
        "summary",
    }


def test_log_format_values() -> None:
    assert LogFormat.JSON.value == "json"
    assert LogFormat.TEXT.value == "text"


def test_no_dataclass_or_to_dict_left_behind() -> None:
    # The old dataclass/to_dict conversion layer must be gone.
    assert not isinstance(ParsedLogEntry, type) or not hasattr(
        ParsedLogEntry, "to_dict"
    )

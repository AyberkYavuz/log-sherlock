"""Architectural guardrails for the shared ``models`` package.

These tests encode the project's model-ownership rules so future refactors
cannot silently reintroduce duplicate definitions or model drift:

    * every shared model has exactly ONE class definition, and it lives in
      ``models/``,
    * feature packages and the graph import those models from ``models`` rather
      than redefining them,
    * the models are ``TypedDict`` s (dict-native graph state).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import graph
import models
import parser.json_parser as json_parser
import parser.text_parser as text_parser
import stats.aggregations as aggregations
from models import LogFormat, ParsedLogEntry, ParserMetrics, Statistics

# Resolved via importlib because the ``parser`` / ``stats`` packages re-export a
# ``parser_node`` / ``statistics_node`` *function*, which shadows the
# same-named submodule attribute; ``import_module`` returns the module object
# regardless.
parser_node_module = importlib.import_module("parser.parser_node")
statistics_node_module = importlib.import_module("stats.statistics_node")

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
    assert files[0] == _REPO_ROOT / "models" / "parsed_log.py"


def test_exactly_one_parser_metrics_definition() -> None:
    files = _count_class_definitions("ParserMetrics")
    assert len(files) == 1, f"expected 1 ParserMetrics definition, found {files}"
    assert files[0] == _REPO_ROOT / "models" / "parser_metrics.py"


def test_exactly_one_statistics_definition() -> None:
    for name in ("Statistics", "CategoryCount", "SeveritySummary", "TimestampCoverage"):
        files = _count_class_definitions(name)
        assert len(files) == 1, f"expected 1 {name} definition, found {files}"
        assert files[0] == _REPO_ROOT / "models" / "statistics.py"


# -- shared imports (identity, not duplication) -----------------------------


def test_parser_imports_shared_models() -> None:
    # The names bound inside the parser modules must be the very objects
    # exported by ``models`` — proving import, not redefinition.
    assert json_parser.ParsedLogEntry is ParsedLogEntry
    assert text_parser.ParsedLogEntry is ParsedLogEntry
    assert parser_node_module.ParsedLogEntry is ParsedLogEntry
    assert parser_node_module.ParserMetrics is ParserMetrics


def test_stats_imports_shared_models() -> None:
    assert statistics_node_module.Statistics is Statistics
    assert statistics_node_module.ParsedLogEntry is ParsedLogEntry
    assert aggregations.CategoryCount is models.CategoryCount


def test_graph_imports_shared_models() -> None:
    assert graph.ParsedLogEntry is ParsedLogEntry
    assert graph.ParserMetrics is ParserMetrics
    assert graph.Statistics is Statistics


def test_models_package_exports() -> None:
    assert set(models.__all__) == {
        "LogFormat",
        "ParsedLogEntry",
        "ParserMetrics",
        "CategoryCount",
        "SeveritySummary",
        "Statistics",
        "TimestampCoverage",
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


def test_log_format_values() -> None:
    assert LogFormat.JSON.value == "json"
    assert LogFormat.TEXT.value == "text"


def test_no_dataclass_or_to_dict_left_behind() -> None:
    # The old dataclass/to_dict conversion layer must be gone.
    assert not isinstance(ParsedLogEntry, type) or not hasattr(
        ParsedLogEntry, "to_dict"
    )

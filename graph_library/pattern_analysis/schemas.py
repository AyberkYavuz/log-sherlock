"""The structured-output schemas this node binds, re-exported for local use.

The definitions themselves live in :mod:`graph_library.models.pattern_analysis`
rather than here, because ``pattern_summary`` crosses a node boundary — the
prepare_output node reads it — and this project keeps every such structure in
the shared ``graph_library.models`` package, defined exactly once. That rule is
enforced by ``tests/test_models_architecture.py``, and the error-analysis node
follows it for its own ``LLMErrorAnalysisResult``.

This module exists so the rest of the package can say "the schemas" in one
import, and so the node's response contract is discoverable from the package it
belongs to. It defines nothing.
"""

from __future__ import annotations

from graph_library.models import (
    AnomalyCategory,
    AnomalySeverity,
    PatternAnalysisResult,
    PatternSummary,
    SystemAnomaly,
    SystemAnomalyRecord,
)

__all__ = [
    "AnomalyCategory",
    "AnomalySeverity",
    "PatternAnalysisResult",
    "PatternSummary",
    "SystemAnomaly",
    "SystemAnomalyRecord",
]

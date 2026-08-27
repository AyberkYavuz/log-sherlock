"""LogSherlock pattern-analysis feature package.

Contains only business logic — the shared models it produces
(:class:`~graph_library.models.PatternSummary`,
:class:`~graph_library.models.PatternAnalysisResult`) live in the shared
``graph_library.models`` package and are imported from there.

The package is laid out by concern, mirroring
:mod:`graph_library.error_analysis`:

    * :mod:`graph_library.pattern_analysis.schemas` — the structured-output
      contract, re-exported from the shared models package,
    * :mod:`graph_library.pattern_analysis.prompts` — serialization of the
      statistics and timeline payloads into prompt context,
    * :mod:`graph_library.pattern_analysis.fallback` — the deterministic
      derivation used when the model cannot be reached,
    * :mod:`graph_library.pattern_analysis.node` — the graph node that ties
      them together.

That split is the same one the error-analysis package makes, and it earns its
keep the same way: everything in ``fallback`` is reproducible arithmetic, and
everything the LLM contributes is confined to ``node``. Because both paths
return a :class:`~graph_library.models.PatternAnalysisResult`, a degraded run
and a healthy one publish the identical shape — the difference shows up in
``investigation_notes``, not in the schema.

This node does not read ``parsed_logs``. It consumes the *output* of the
statistics and timeline nodes, which is why the graph runs it downstream of
both rather than as a fourth parallel branch.

Public surface:

    * :func:`pattern_analysis_node` — the graph node entry point.
    * :func:`build_pattern_analysis_prompt`, :func:`build_fallback_summary` and
      the helpers below — for reuse and testing.
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

from .fallback import (
    DOMINANCE_RATIO,
    MAX_CASCADE_LOGGERS,
    MAX_METADATA_INSIGHTS,
    SEVERITY_THRESHOLDS,
    SPIKE_RATIO,
    build_fallback_summary,
    severity_for_error_share,
)
from .node import NO_INPUT_NOTE, pattern_analysis_node
from .prompts import (
    MAX_INVESTIGATION_NOTES,
    MAX_TIMELINE_BUCKETS,
    STATISTICS_FIELDS,
    SYSTEM_PROMPT,
    TIMELINE_FIELDS,
    build_pattern_analysis_prompt,
    format_investigation_notes,
    format_statistics,
    format_timeline,
    prompt_payload_sizes,
)

__all__ = [
    "pattern_analysis_node",
    # schemas (re-exported from graph_library.models)
    "PatternAnalysisResult",
    "PatternSummary",
    "SystemAnomaly",
    "SystemAnomalyRecord",
    "AnomalyCategory",
    "AnomalySeverity",
    # prompts
    "build_pattern_analysis_prompt",
    "format_statistics",
    "format_timeline",
    "format_investigation_notes",
    "prompt_payload_sizes",
    "SYSTEM_PROMPT",
    "STATISTICS_FIELDS",
    "TIMELINE_FIELDS",
    "MAX_TIMELINE_BUCKETS",
    "MAX_INVESTIGATION_NOTES",
    # fallback
    "build_fallback_summary",
    "severity_for_error_share",
    "SEVERITY_THRESHOLDS",
    "SPIKE_RATIO",
    "DOMINANCE_RATIO",
    "MAX_METADATA_INSIGHTS",
    "MAX_CASCADE_LOGGERS",
    # node
    "NO_INPUT_NOTE",
]

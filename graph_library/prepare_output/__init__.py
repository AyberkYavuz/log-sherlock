"""LogSherlock prepare-output feature package.

Contains only business logic — the shared models it produces
(:class:`~graph_library.models.StructuredInvestigationReport` and its sections,
:class:`~graph_library.models.prepare_output.LLMPrepareOutputResult`) live in
the shared ``graph_library.models`` package and are imported from there.

The package is laid out by concern, mirroring
:mod:`graph_library.pattern_analysis`:

    * :mod:`graph_library.prepare_output.scoring` — the deterministic
      confidence engine,
    * :mod:`graph_library.prepare_output.prompts` — assembly of five upstream
      payloads into one prompt,
    * :mod:`graph_library.prepare_output.node` — the graph node that ties them
      together.

The split is the one the sibling nodes make, and it earns its keep the same way:
everything in ``scoring`` is reproducible arithmetic, and everything the LLM
contributes is confined to ``node``. Because both paths return a complete
``structured_report``, a degraded run and a healthy one publish the identical
shape — the difference shows up in the confidence score and in
``investigation_notes``, not in the schema.

This node does not read ``parsed_logs``. It consumes the *output* of the four
analysis stages plus ``parser_metrics``, which is why the graph registers it
with ``defer=True`` and holds it until every branch has landed.

Public surface:

    * :func:`prepare_output_node` — the graph node entry point.
    * :func:`compute_confidence_score`, :func:`build_prepare_output_prompt` and
      the helpers below — for reuse and testing.
"""

from __future__ import annotations

from graph_library.models.prepare_output import (
    LLMPrepareOutputResult,
    StructuredAIInsights,
    StructuredDeterministicOutputs,
    StructuredInvestigationReport,
    StructuredReportMetadata,
    StructuredSynthesis,
)

from .node import (
    CONFIDENCE_DIVERGENCE_THRESHOLD,
    FALLBACK_EXECUTIVE_SUMMARY,
    FALLBACK_ROOT_CAUSE,
    NO_INPUT_NOTE,
    prepare_output_node,
)
from .prompts import (
    ERROR_SIGNATURE_FIELDS,
    MAX_ERROR_SIGNATURES,
    MAX_HISTORICAL_CHARS,
    MAX_HISTORICAL_INVESTIGATIONS,
    PARSER_METRIC_FIELDS,
    SYSTEM_PROMPT,
    build_prepare_output_prompt,
    format_error_summary,
    format_historical_context,
    format_parser_health,
    format_pattern_summary,
    prompt_payload_sizes,
)
from .scoring import (
    AMBIGUOUS_ROOT_CAUSE_PENALTY,
    BASE_SCORE,
    FALLBACK_PENALTY,
    LOW_PARSER_CONFIDENCE_PENALTY,
    MALFORMED_PENALTY_PER_PERCENT,
    MAX_SCORE,
    MIN_PARSER_CONFIDENCE,
    MIN_SCORE,
    MISSING_TIMESTAMP_PERCENT_PER_POINT,
    apply_fallback_penalty,
    compute_confidence_score,
    confidence_breakdown,
)

__all__ = [
    "prepare_output_node",
    # schemas (re-exported from graph_library.models)
    "StructuredInvestigationReport",
    "StructuredReportMetadata",
    "StructuredSynthesis",
    "StructuredDeterministicOutputs",
    "StructuredAIInsights",
    "LLMPrepareOutputResult",
    # scoring
    "compute_confidence_score",
    "confidence_breakdown",
    "apply_fallback_penalty",
    "BASE_SCORE",
    "MIN_SCORE",
    "MAX_SCORE",
    "MALFORMED_PENALTY_PER_PERCENT",
    "MISSING_TIMESTAMP_PERCENT_PER_POINT",
    "AMBIGUOUS_ROOT_CAUSE_PENALTY",
    "LOW_PARSER_CONFIDENCE_PENALTY",
    "MIN_PARSER_CONFIDENCE",
    "FALLBACK_PENALTY",
    # prompts
    "build_prepare_output_prompt",
    "format_parser_health",
    "format_error_summary",
    "format_pattern_summary",
    "format_historical_context",
    "prompt_payload_sizes",
    "SYSTEM_PROMPT",
    "ERROR_SIGNATURE_FIELDS",
    "PARSER_METRIC_FIELDS",
    "MAX_ERROR_SIGNATURES",
    "MAX_HISTORICAL_INVESTIGATIONS",
    "MAX_HISTORICAL_CHARS",
    # node
    "FALLBACK_ROOT_CAUSE",
    "FALLBACK_EXECUTIVE_SUMMARY",
    "NO_INPUT_NOTE",
    "CONFIDENCE_DIVERGENCE_THRESHOLD",
]

"""LogSherlock error-analysis feature package.

Contains only business logic — the shared models it produces
(:class:`~models.ErrorSignature`, :class:`~models.ErrorSummary`) live in the
shared ``models`` package and are imported from there.

The package is laid out by concern:

    * :mod:`error_analysis.fingerprint` — deterministic severity filtering,
      traceback collation, parameter masking and signature grouping,
    * :mod:`error_analysis.llm_factory` — provider/mode to chat-model routing,
    * :mod:`error_analysis.node` — the graph node that ties the two together.

The split mirrors the node's two passes. Everything in ``fingerprint`` is
reproducible arithmetic; everything the LLM contributes is confined to
``node``. That boundary is what makes the counted findings trustworthy even
when the model is unavailable.

When the caller sets ``enable_web_search`` the node gains a third, optional
step in front of the other two — a decision call that asks whether any
signature is obscure enough to look up. Only the decision lives here; the
lookup itself is the separate :mod:`web_search` package, and the graph routes
between them.

Public surface:

    * :func:`error_analysis_node` — the graph node entry point.
    * :func:`build_error_summary`, :func:`get_error_analysis_llm` and the
      helpers below — for reuse and testing.
"""

from __future__ import annotations

from .fingerprint import (
    ERROR_SEVERITIES,
    MAX_SIGNATURES_FOR_LLM,
    NO_ERRORS_NOTE,
    SAMPLE_MESSAGE_LIMIT,
    SAMPLE_MESSAGE_MAX_LENGTH,
    TEMPLATE_MAX_LENGTH,
    WARNING_SEVERITIES,
    build_error_summary,
    collate_message,
    is_continuation,
    mask_message,
    select_error_entries,
)
from .llm_factory import (
    ANTHROPIC_MAX_TOKENS,
    ANTHROPIC_TEMPERATURE_MODELS,
    DEEPSEEK_BASE_URL,
    DEFAULT_LOCAL_API_KEY,
    DEFAULT_LOCAL_BASE_URL,
    DEFAULT_LOCAL_MODEL,
    DEFAULT_MODE,
    DEFAULT_PROVIDER,
    MODEL_DISCOVERY_TIMEOUT,
    MODEL_FALLBACKS,
    MODEL_TIERS,
    PROVIDER_ALIASES,
    STRUCTURED_OUTPUT_OVERRIDES,
    TEMPERATURE,
    anthropic_supports_temperature,
    clear_model_discovery_cache,
    discover_models,
    get_error_analysis_llm,
    is_model_unavailable,
    iter_error_analysis_llms,
    normalize_mode,
    normalize_provider,
    resolve_model_candidates,
    resolve_model_name,
    structured_output_kwargs,
    supports_temperature,
)
from .node import (
    SEARCH_DECISION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_analysis_prompt,
    build_search_decision_prompt,
    decide_search_queries,
    error_analysis_node,
)

__all__ = [
    "error_analysis_node",
    # fingerprint
    "build_error_summary",
    "select_error_entries",
    "mask_message",
    "collate_message",
    "is_continuation",
    "ERROR_SEVERITIES",
    "WARNING_SEVERITIES",
    "MAX_SIGNATURES_FOR_LLM",
    "SAMPLE_MESSAGE_LIMIT",
    "SAMPLE_MESSAGE_MAX_LENGTH",
    "TEMPLATE_MAX_LENGTH",
    "NO_ERRORS_NOTE",
    # llm_factory
    "get_error_analysis_llm",
    "iter_error_analysis_llms",
    "resolve_model_name",
    "resolve_model_candidates",
    "normalize_provider",
    "normalize_mode",
    "discover_models",
    "clear_model_discovery_cache",
    "is_model_unavailable",
    "structured_output_kwargs",
    "STRUCTURED_OUTPUT_OVERRIDES",
    "PROVIDER_ALIASES",
    "MODEL_FALLBACKS",
    "MODEL_DISCOVERY_TIMEOUT",
    "supports_temperature",
    "anthropic_supports_temperature",
    "ANTHROPIC_TEMPERATURE_MODELS",
    "ANTHROPIC_MAX_TOKENS",
    "MODEL_TIERS",
    "DEFAULT_PROVIDER",
    "DEFAULT_MODE",
    "DEFAULT_LOCAL_MODEL",
    "DEFAULT_LOCAL_BASE_URL",
    "DEFAULT_LOCAL_API_KEY",
    "DEEPSEEK_BASE_URL",
    "TEMPERATURE",
    # node
    "build_analysis_prompt",
    "SYSTEM_PROMPT",
    # node — optional web-search decision pass
    "decide_search_queries",
    "build_search_decision_prompt",
    "SEARCH_DECISION_SYSTEM_PROMPT",
]

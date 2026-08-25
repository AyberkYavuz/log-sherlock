"""LogSherlock web-search feature package.

An optional capability, off unless the caller sets ``enable_web_search``. It
retrieves external documentation for the error signatures a model cannot be
expected to recognise — obscure vendor codes, framework internals, ecosystem
quirks — while leaving the ordinary case exactly as fast and as deterministic
as it was before.

The package is laid out by concern:

    * :mod:`graph_library.web_search.client` — Tavily construction, query execution,
      relevance filtering and snippet formatting,
    * :mod:`graph_library.web_search.node` — the graph node, which adds the error handling
      that keeps a failed search from being a failed investigation.

It produces no shared model: its contribution to graph state is a
``list[str]`` of prompt-ready snippets, so there is nothing for the ``graph_library.models``
package to define. The schema for the *decision* that precedes it
(:class:`~graph_library.models.LLMSearchDecision`) belongs to the Error Analysis Node, which
is what makes it, and lives in ``graph_library.models`` with that node's other
schemas.

Public surface:

    * :func:`web_search_node` — the graph node entry point.
    * :func:`run_web_search`, :func:`build_client` and the tuning constants
      below — for reuse and testing.
"""

from __future__ import annotations

from .client import (
    API_KEY_ENV_VAR,
    MAX_RESULTS_PER_QUERY,
    MIN_RELEVANCE_SCORE,
    SEARCH_DEPTH,
    SEARCH_TIMEOUT,
    SNIPPET_MAX_LENGTH,
    build_client,
    format_result,
    run_web_search,
)
from .node import web_search_node

__all__ = [
    "web_search_node",
    # client
    "run_web_search",
    "build_client",
    "format_result",
    "SEARCH_DEPTH",
    "MAX_RESULTS_PER_QUERY",
    "MIN_RELEVANCE_SCORE",
    "SNIPPET_MAX_LENGTH",
    "SEARCH_TIMEOUT",
    "API_KEY_ENV_VAR",
]

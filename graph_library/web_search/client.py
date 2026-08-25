"""Tavily access for the Web Search Node — the only module that talks to it.

Everything vendor-specific lives here: how the client is constructed, how a
query is executed, which results survive, and how a surviving result becomes
one line of prompt context. :mod:`graph_library.web_search.node` knows none of it; it calls
:func:`run_web_search` and puts the strings it gets back into graph state.

Three rules shape the module:

    * **The credential comes from the environment**, never from graph state, so
      an API key cannot leak into a report or a checkpoint. This matches
      :mod:`graph_library.error_analysis.llm_factory`, which holds the same line for the LLM
      providers.
    * **The SDK is imported lazily**, inside the function that needs it, so a
      deployment that never enables web search does not have to install
      ``tavily-python``.
    * **A relevance floor is enforced here, not in the prompt.** Tavily always
      returns results — an invented error code still comes back with three
      pages about unrelated memory errors, scoring 0.12-0.35 where a genuine
      match scores 0.6 and above. Passing those on would not merely waste
      tokens, it would invite the model to explain an error using documentation
      for a different one. :data:`MIN_RELEVANCE_SCORE` is the line between the
      two, measured against the live API.

Failures are reported by raising, not by returning something empty. The two
are not the same thing — "the search ran and nothing was relevant" is a real
answer about the logs, while "no API key" is a deployment problem — and only
the node is in a position to decide that neither should stop the graph.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: Tavily's cheap tier. ``"advanced"`` costs more credits and more latency for
#: a deeper crawl, which buys nothing here: this node wants the summary
#: paragraph off a documentation page, not a thorough read of the whole site.
SEARCH_DEPTH = "basic"

#: Results requested per query, before the relevance floor is applied.
MAX_RESULTS_PER_QUERY = 3

#: The score below which a Tavily result is treated as noise rather than
#: documentation. Calibrated against the live API: a query that genuinely has
#: an answer returns 0.6-0.7, while a query for a made-up error code returns
#: 0.12-0.35 worth of loosely-related pages. 0.4 sits in the empty band between
#: them.
#:
#: A query whose every result falls below the floor contributes *nothing* and
#: says so in a note. There is deliberately no "keep the best one anyway"
#: fallback — the best of three irrelevant pages is still irrelevant, and this
#: floor is the only thing standing between a hallucinated error code and a
#: confidently wrong explanation sourced from a Windows bluescreen guide.
MIN_RELEVANCE_SCORE = 0.4

#: Characters of page content kept per result. Tavily returns 500-1500; three
#: queries at three results each would otherwise add ~13k characters to a
#: prompt that already carries every error signature.
SNIPPET_MAX_LENGTH = 500

#: Seconds to wait on a single query. The node runs in parallel with three
#: others and everything downstream waits on the slowest branch, so a hung
#: search must fail rather than hold up the investigation.
SEARCH_TIMEOUT = 20.0

#: The environment variable holding the Tavily credential.
API_KEY_ENV_VAR = "TAVILY_API_KEY"


def build_client(api_key: str | None = None) -> Any:
    """Construct a ``TavilyClient``.

    Args:
        api_key: Use this credential instead of the environment's. Intended
            for tests; production callers pass nothing and let
            :data:`API_KEY_ENV_VAR` supply it.

    Returns:
        A ready ``TavilyClient``. Construction contacts nothing, so a bad key
        builds cleanly and only fails on the first query.

    Raises:
        ImportError: If ``tavily-python`` is not installed. The message names
            the package to install.
        RuntimeError: If no credential is available. Raised rather than
            returning a client that would fail on every query with a less
            obvious message.
    """
    try:
        from tavily import TavilyClient
    except ImportError as exc:  # pragma: no cover - depends on environment
        logger.error("tavily-python is not installed", exc_info=True)
        raise ImportError(
            "Web search requires the tavily-python package. Install it with: "
            "pip install tavily-python"
        ) from exc

    key = api_key or os.getenv(API_KEY_ENV_VAR)
    if not key:
        raise RuntimeError(
            f"Web search is enabled but {API_KEY_ENV_VAR} is not set. Set it, "
            "or leave enable_web_search off."
        )

    logger.debug("Tavily client constructed (api_key=<set>)")
    return TavilyClient(api_key=key)


def _truncate(text: str) -> str:
    """Clip page content to :data:`SNIPPET_MAX_LENGTH`, marking the cut."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= SNIPPET_MAX_LENGTH:
        return collapsed
    return f"{collapsed[:SNIPPET_MAX_LENGTH].rstrip()}..."


def format_result(query: str, result: dict[str, Any]) -> str:
    """Render one Tavily result as a single prompt-ready snippet.

    The query is carried in the snippet rather than being dropped once the
    search is over: the model reads several snippets from several queries at
    once, and which question a page was answering is what tells it which
    signature the page is about.

    The URL is included for the same reason a citation is — the explanation
    the model writes ends up in a report a human has to trust.

    Args:
        query: The query that produced this result.
        result: One entry from Tavily's ``results`` list.

    Returns:
        A compact multi-line snippet.
    """
    title = str(result.get("title") or "Untitled").strip()
    url = str(result.get("url") or "").strip()
    content = _truncate(str(result.get("content") or ""))

    return f"[query: {query}]\n{title} — {url}\n{content}"


def _relevant_results(query: str, payload: dict[str, Any]) -> list[str]:
    """Filter one query's payload down to the snippets worth prompting with."""
    results = payload.get("results")
    if not isinstance(results, list):
        logger.warning(
            "Tavily returned no results list for query %r; ignoring it", query
        )
        return []

    kept: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        # A result without a score is kept: the floor exists to reject results
        # Tavily itself rates poorly, and absence of a rating is not a poor one.
        score = result.get("score")
        if isinstance(score, (int, float)) and score < MIN_RELEVANCE_SCORE:
            logger.debug(
                "Dropping result for query %r: score %.3f is below %.2f (%s)",
                query,
                score,
                MIN_RELEVANCE_SCORE,
                result.get("url"),
            )
            continue
        kept.append(format_result(query, result))

    logger.info(
        "Query %r: kept %d of %d result(s) at or above score %.2f",
        query,
        len(kept),
        len(results),
        MIN_RELEVANCE_SCORE,
    )
    return kept


def run_web_search(
    queries: list[str], *, client: Any | None = None
) -> tuple[list[str], list[str]]:
    """Run every query and return the snippets worth putting in a prompt.

    Queries are independent, so one failing does not cancel the others: a
    single unreachable request loses that query's context and is reported,
    while the rest of the search still contributes. Only a failure to build
    the client at all — no package, no credential — stops everything, because
    it would stop every query identically.

    Args:
        queries: The query strings from the decision pass. Blank entries and
            duplicates are dropped; an empty list short-circuits without
            constructing a client.
        client: A pre-built client, chiefly for tests. Omit it in production
            and let :func:`build_client` read the environment.

    Returns:
        A ``(snippets, notes)`` pair. ``snippets`` is the flat, prompt-ready
        context in query order. ``notes`` records queries that failed or
        returned nothing relevant, for ``investigation_notes``.

    Raises:
        ImportError: If ``tavily-python`` is not installed.
        RuntimeError: If no Tavily credential is available.
    """
    # Deduplicated because the decision pass is a language model and asking the
    # same question twice is a thing they do; each duplicate would otherwise
    # cost a round trip to be told the same thing.
    wanted = list(dict.fromkeys(query.strip() for query in queries if query.strip()))
    if not wanted:
        logger.info("No usable queries to run")
        return [], []

    active = client if client is not None else build_client()

    snippets: list[str] = []
    notes: list[str] = []

    for query in wanted:
        logger.info(
            "Searching Tavily: query=%r depth=%s max_results=%d",
            query,
            SEARCH_DEPTH,
            MAX_RESULTS_PER_QUERY,
        )
        try:
            payload = active.search(
                query,
                search_depth=SEARCH_DEPTH,
                max_results=MAX_RESULTS_PER_QUERY,
                timeout=SEARCH_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - one dead query must not sink the rest
            logger.warning(
                "Tavily query %r failed (%s: %s); continuing with the remaining "
                "queries",
                query,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            notes.append(
                f"Web search: query {query!r} failed ({type(exc).__name__}); no "
                "external context was retrieved for it."
            )
            continue

        relevant = _relevant_results(query, payload if isinstance(payload, dict) else {})
        if not relevant:
            notes.append(
                f"Web search: query {query!r} returned nothing above the "
                f"relevance threshold ({MIN_RELEVANCE_SCORE}); it contributed "
                "no context."
            )
            continue

        snippets.extend(relevant)

    logger.info(
        "Web search complete: %d query/queries produced %d snippet(s)",
        len(wanted),
        len(snippets),
    )
    return snippets, notes

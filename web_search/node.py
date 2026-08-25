"""The Web Search Node — the optional detour between the two analysis passes.

The node exists because the Error Analysis Node's model knows the common
failures cold and the rare ones not at all. A connection refused needs no
research; a framework-internal panic code that appears in one vendor's changelog
and nowhere else does. Pass 1 of :mod:`error_analysis.node` decides which case
it is looking at and, when it is the second, leaves queries in state. This node
answers them.

It contributes exactly one field, ``search_context``, and it **always**
contributes it — a list, never ``None``, on every path including every failure
path. That is not tidiness, it is the loop's termination condition: the router
in :mod:`graph` sends error analysis back here only while ``search_context`` is
``None``, so a node that declined to write the field on failure would search,
fail, and be routed straight back to search again.

Failure is expected rather than exceptional here, because this is the one node
that depends on the public internet and on a credential the operator may simply
not have set. A missing ``TAVILY_API_KEY``, an uninstalled SDK, a network that
is down: each produces an empty context, a note saying which, and a graph that
keeps running. The investigation is worse off by exactly the documentation it
could not fetch, and no more.
"""

from __future__ import annotations

import logging
from typing import Any

from .client import run_web_search

logger = logging.getLogger(__name__)


def web_search_node(state: dict[str, Any]) -> dict[str, Any]:
    """Answer the queries the error-analysis decision pass asked for.

    Args:
        state: The LogSherlock graph state. Reads ``search_queries`` (what to
            look up) and nothing else. Treated as read-only.

    Returns:
        A partial state delta containing exactly:

            * ``search_context`` — the retrieved snippets, ``[]`` when there
              was nothing to search for, nothing relevant to be found, or
              nothing reachable,
            * ``investigation_notes`` — what was searched and what failed,
            * ``completed_stages`` — ``["web_search"]``.

        Never raises. See the module docstring for why ``search_context`` is
        always present.
    """
    queries = state.get("search_queries") or []

    logger.info("Web search node starting: queries=%d", len(queries))

    if not queries:
        # Reachable if the router is bypassed — the node called directly, or a
        # caller seeding state by hand. Writing the field anyway keeps the
        # contract above true.
        logger.info("No search queries in state; nothing to retrieve")
        return {
            "search_context": [],
            "investigation_notes": [],
            "completed_stages": ["web_search"],
        }

    try:
        snippets, notes = run_web_search(list(queries))
    except Exception as exc:  # noqa: BLE001 - an empty context beats a dead graph
        # Everything run_web_search raises is a deployment fact rather than a
        # fact about the logs: no package, no credential. It is logged in full
        # because the note below is a summary, and a run that searched nothing
        # otherwise looks identical to one that found nothing.
        logger.error(
            "Web search unavailable; continuing without external context",
            exc_info=True,
        )
        return {
            "search_context": [],
            "investigation_notes": [
                f"Web search: unavailable ({type(exc).__name__}: {exc}). The "
                "analysis continued without external documentation."
            ],
            "completed_stages": ["web_search"],
        }

    summary = (
        f"Web search: ran {len(queries)} "
        f"{'query' if len(queries) == 1 else 'queries'} and retrieved "
        f"{len(snippets)} relevant "
        f"{'snippet' if len(snippets) == 1 else 'snippets'}."
    )
    logger.info("%s", summary)

    return {
        "search_context": snippets,
        "investigation_notes": [summary, *notes],
        "completed_stages": ["web_search"],
    }

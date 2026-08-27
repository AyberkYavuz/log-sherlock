"""LogSherlock — multi-agent log analysis graph.

This module defines *only* the graph architecture for LogSherlock:

    * the graph state (input / working / output),
    * the reducers used to merge concurrent updates,
    * placeholder node definitions,
    * the fixed graph topology,
    * a ``compile_graph()`` factory.

No business logic lives here: implemented nodes are built in their own feature
packages under ``graph_library/`` (``graph_library/parser/``,
``graph_library/stats/``, ``graph_library/timeline/``,
``graph_library/pattern_analysis/``, ``graph_library/error_analysis/``,
``graph_library/web_search/``) and merely registered below.
Every node that is not implemented yet is a deterministic stub that documents
its future responsibility via a ``TODO`` block and returns an empty state
delta. Recommendation logic and report rendering are intentionally left
unimplemented.

Topology (fixed workflow)::

    START
      -> parser -----------------------------------------------------------------------+
      -> [ error_analysis (LLM) <-> web_search (network) ] ---------------------------+|
      -> [ statistics (deterministic), timeline (deterministic) ]                     ||
             |                                       |                                ||
             +---------------------------------------+-> pattern_analysis (LLM) ------++-> recommendation -> report_generator -> END
             |                                                                        |
             +------------------------------------------------------------------------+

``parser`` fans out into three analysis branches. ``error_analysis`` runs on its
own (with the optional web-search detour); ``statistics`` and ``timeline`` are
the deterministic pair, and ``pattern_analysis`` runs only once *both* have
landed, because it reasons over their output rather than over ``parsed_logs``.

``recommendation`` is the fan-in for all four analysis stages *and* for the
parser itself: the fourth edge out of ``parser`` carries ``parser_metrics``
straight to it, so its conclusions can be qualified by how much of the payload
was actually readable. No analysis stage forwards those metrics — each one
consumes what it needs and publishes its own artifact — so without the direct
edge the fan-in would have no view of ingestion health.

The one deviation from "fixed" is the ``error_analysis <-> web_search`` loop,
and it is bounded to a single lap: ``web_search`` always writes
``search_context``, and the router only sends work its way while that field is
``None``. Off by default — see ``enable_web_search``.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

# Shared graph models live in the dedicated ``graph_library.models`` package —
# the single source of truth for every structure that crosses a node boundary.
# Feature packages (parser, statistics, ...) import from here; they never
# redefine.
from graph_library.models import (
    AnalysisMode,
    ErrorSummary,
    LLMProvider,
    ParsedLogEntry,
    ParserMetrics,
    PatternSummary,
    Statistics,
    TimelineEvent,
)

# The error_analysis, parser, pattern_analysis, statistics, timeline and
# web_search nodes are implemented as standalone feature packages under
# ``graph_library/``. They are imported here and registered directly in
# ``build_graph`` — this module defines no stub for them.
from graph_library.error_analysis import error_analysis_node
from graph_library.parser import parser_node
from graph_library.pattern_analysis import pattern_analysis_node
from graph_library.stats import statistics_node
from graph_library.timeline import timeline_node
from graph_library.web_search.node import web_search_node

# ---------------------------------------------------------------------------
# Payload type aliases
# ---------------------------------------------------------------------------
# Concrete models (``ParsedLogEntry``, ``ParserMetrics``, ``Statistics``,
# ``TimelineEvent``, ``ErrorSummary``, ``PatternSummary``) now live in the
# shared ``models`` package. The alias below remains a deliberately loose
# (``dict[str, Any]``) placeholder for a payload whose node is not yet
# implemented; it should graduate into a ``models`` module as that node lands,
# the way ``PatternSummary`` did when the pattern-analysis node was built.

HistoricalInvestigation = dict[str, Any]


class ExecutionMetadata(TypedDict, total=False):
    """Placeholder for runtime metadata about a single graph execution.

    Intentionally left unpopulated for Version 1. It exists so the state has a
    stable home for observability data that the runtime will attach later.

    TODO: populate at runtime with fields such as::

        graph_version: str
        model_name: str
        token_usage: dict[str, int]
        latency_ms: int
        execution_time: str
        estimated_cost: float
    """


class StructuredInvestigationReport(TypedDict, total=False):
    """Placeholder for the machine-readable investigation report.

    Deliberately empty for now. This is the type we intend to grow into the
    persisted database model, so it gets a dedicated name (rather than a loose
    ``dict[str, Any]``) from day one.

    TODO: define the concrete report schema here, e.g. application identity,
    root cause, confidence, key errors/patterns, timeline highlights and the
    historical comparison — then reuse it as the storage/DB model.
    """


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------
class LogSherlockState(TypedDict, total=False):
    """Shared state passed between every node.

    Conceptually partitioned into three regions:

    * **INPUT**   — supplied by the caller, treated as read-only by the graph.
    * **WORKING** — intermediate artifacts produced while the graph runs.
    * **OUTPUT**  — the final deliverables returned to the caller.

    ``total=False`` because the caller only provides the INPUT region; the
    WORKING and OUTPUT regions are populated progressively as nodes execute.
    """

    # ---- INPUT ------------------------------------------------------------
    # Version 1 identifies applications by name only; the backend can introduce
    # UUIDs / stable ids later without touching the graph contract.
    application_name: str
    raw_logs: str
    investigation_timestamp: str
    analysis_mode: AnalysisMode
    # Which vendor the LLM nodes should call. Paired with ``analysis_mode``,
    # which selects the model *tier* within that vendor. Both are optional:
    # each LLM node applies its own documented default (``"openai"`` /
    # ``"standard"``) when the caller omits them.
    llm_provider: LLMProvider
    # Summaries from previous investigations, supplied as graph input.
    # NOTE: we deliberately do *not* use LangGraph's memory/checkpointer for
    # history — the caller owns persistence and passes context in explicitly.
    historical_context: list[HistoricalInvestigation]

    # Opt in to the web-search detour described in the module docstring.
    # Absent (the default) is ``False``: the capability trades latency, cost
    # and determinism for coverage of unfamiliar errors, and that is the
    # caller's trade to make, not the graph's. A ``TypedDict`` cannot carry a
    # default, so every reader applies ``bool(state.get(...))``.
    enable_web_search: bool

    # ---- WORKING ----------------------------------------------------------
    # Both fields below are written solely by the parser node (single writer),
    # so they need no reducer. ``parser_metrics`` exposes structured parser
    # health for deterministic downstream use (e.g. the statistics node reads
    # it instead of recomputing parser health from ``raw_logs``).
    parsed_logs: list[ParsedLogEntry]
    parser_metrics: ParserMetrics

    # The four analysis nodes each own exactly one of the fields below and are
    # the *sole* writer of that field. Because there is no write contention on
    # any single key, these fields need NO reducer — LangGraph merges disjoint
    # keys from concurrent branches automatically. (See
    # ``investigation_notes`` / ``completed_stages`` for the fields that
    # genuinely require a reducer.)
    error_summary: ErrorSummary       # written by error_analysis_node
    pattern_summary: PatternSummary   # written by pattern_analysis_node
    statistics: Statistics            # written by statistics_node
    timeline: list[TimelineEvent]     # written by timeline_node

    # The two halves of the web-search handshake, each with a single writer:
    # error_analysis asks, web_search answers. They need no reducer for the
    # same reason as the four fields above — no two nodes write either key.
    #
    # ``search_context`` does double duty as the loop's state marker, and the
    # distinction between its two falsy values is load-bearing:
    #
    #     None -> nobody has decided yet; error_analysis will run its decision
    #             pass, and the router may send state to web_search;
    #     []   -> decided, with nothing to show for it — no signature was
    #             obscure enough, the search found nothing relevant, or it
    #             could not run at all. The analysis proceeds unenriched and
    #             the loop is over.
    #
    # Anything that writes this field must therefore write a list, never None.
    search_queries: list[str]         # written by error_analysis_node (pass 1)
    search_context: list[str]         # written by web_search_node

    # Concurrent-safe observability channels. Any node (including the four
    # parallel branches running in the same superstep) may append here, so
    # these MUST use an additive reducer to avoid the "concurrent update to
    # the same key" error and to preserve every contribution.
    #
    # ``investigation_notes`` is broader than "warnings" — it collects parser
    # notes, skipped lines, missing timestamps, unsupported formats, "historical
    # comparison unavailable" and other general observations.
    investigation_notes: Annotated[list[str], operator.add]
    completed_stages: Annotated[list[str], operator.add]

    # ---- OUTPUT -----------------------------------------------------------
    executive_summary: str
    root_cause: str
    # Confidence expressed on a 0–100 integer scale (not 0.0–1.0) because a
    # whole-number percentage is easier for users to read and reason about.
    confidence_score: int
    markdown_report: str
    structured_report: StructuredInvestigationReport

    # Optional runtime metadata; defined now, populated by the runtime later.
    execution_metadata: ExecutionMetadata


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
# Every node accepts the full state and returns a *partial* state delta. Nodes
# never mutate the incoming state in place — they return only the keys they own.


def recommendation_node(state: LogSherlockState) -> LogSherlockState:
    """LLM agent that synthesizes findings into a root cause + recommendation.

    This is the fan-in point: it reads every WORKING artifact produced by the
    four analysis stages and compares them against ``historical_context``.

    It also takes a direct edge from ``parser`` in order to read
    ``parser_metrics``, which no analysis stage forwards. Those metrics are what
    let a conclusion be qualified rather than merely stated: a root cause
    inferred from a payload where a third of the lines were malformed, or where
    most entries carried no timestamp, deserves a lower ``confidence_score`` and
    an explicit caveat in the summary.

    TODO:
        * Fuse error_summary, pattern_summary, statistics and timeline.
        * Weigh the findings against ``parser_metrics`` — malformed-line and
          missing-timestamp counts bound how much the analysis can be trusted.
        * Compare current signals against prior investigations (regressions,
          recurring issues, drift) from ``historical_context``.
        * Infer the most likely ``root_cause`` and a ``confidence_score``,
          discounted for poor ingestion health.
        * Draft the ``executive_summary``, surfacing any data-quality caveat.
    """
    return {
        "executive_summary": "",
        "root_cause": "",
        "confidence_score": 0,
        "completed_stages": ["recommendation"],
    }


def report_generator_node(state: LogSherlockState) -> LogSherlockState:
    """LLM node that renders the final human- and machine-readable reports.

    TODO:
        * Render a polished ``markdown_report`` for humans.
        * Assemble a ``structured_report`` (JSON-serializable) for downstream
          systems and for storage as future ``historical_context``.
    """
    return {
        "markdown_report": "",
        "structured_report": {},
        "completed_stages": ["report_generator"],
    }


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

# The four analysis stages, in the order they appear in the topology diagram.
# Declared once so the topology stays readable and DRY.
_ANALYSIS_NODES: tuple[str, ...] = (
    "error_analysis",
    "statistics",
    "timeline",
    "pattern_analysis",
)

# The analysis branches ``parser`` fans out into. ``pattern_analysis`` is absent
# because it consumes the deterministic pair below rather than ``parsed_logs``.
# Not the complete set of edges out of ``parser`` — it also feeds
# ``recommendation`` directly, which is a fan-in edge rather than a branch.
_PARSER_FANOUT: tuple[str, ...] = ("error_analysis", "statistics", "timeline")

# The deterministic nodes ``pattern_analysis`` reasons over. It runs only once
# both have landed.
_PATTERN_ANALYSIS_INPUTS: tuple[str, ...] = ("statistics", "timeline")

# The analysis stages whose route to ``recommendation`` is a plain edge.
# ``error_analysis`` is missing because it routes conditionally — see
# ``route_after_error_analysis``.
_DIRECT_TO_RECOMMENDATION: tuple[str, ...] = tuple(
    node for node in _ANALYSIS_NODES if node != "error_analysis"
)


def route_after_error_analysis(state: LogSherlockState) -> str:
    """Decide whether error analysis needs a web search before it can finish.

    Called after every ``error_analysis`` run, and reads the delta that run
    just merged:

        * queries asked for and nothing retrieved yet -> ``"web_search"``;
        * anything else -> ``"recommendation"``.

    The condition is what bounds the loop. ``web_search_node`` always writes
    ``search_context`` — a list on every path, including every failure path —
    so the second time this function sees the same state the field is no longer
    ``None`` and the branch terminates. A search node that left the field unset
    on failure would be routed straight back into another search.

    Args:
        state: The graph state, after the error-analysis delta is applied.

    Returns:
        The name of the next node.
    """
    if state.get("search_queries") and state.get("search_context") is None:
        return "web_search"
    return "recommendation"


def build_graph() -> StateGraph:
    """Construct the (uncompiled) ``StateGraph`` with all nodes and edges."""
    builder = StateGraph(LogSherlockState)

    # -- register nodes -----------------------------------------------------
    builder.add_node("parser", parser_node)
    builder.add_node("error_analysis", error_analysis_node)
    builder.add_node("web_search", web_search_node)
    builder.add_node("pattern_analysis", pattern_analysis_node)
    builder.add_node("statistics", statistics_node)
    builder.add_node("timeline", timeline_node)
    # ``defer`` holds this node back until no other task is pending, which is
    # what keeps the fan-in a fan-in now that the branches no longer finish
    # together: one can take a web-search detour and one runs a superstep
    # later than the pair it consumes. Without it the join fires as soon as
    # the plain edges below have been written — LangGraph treats the
    # conditional branch out of ``error_analysis`` as a separate trigger
    # rather than a member of the join — and ``recommendation`` runs twice:
    # once on an incomplete state, then again once the rest lands.
    builder.add_node("recommendation", recommendation_node, defer=True)
    builder.add_node("report_generator", report_generator_node)

    # -- entry --------------------------------------------------------------
    builder.add_edge(START, "parser")

    # -- fan-out ------------------------------------------------------------
    # parser fans out into parallel branches: the error-analysis chain and the
    # deterministic pair, all in one superstep.
    for node in _PARSER_FANOUT:
        builder.add_edge("parser", node)

    # -- the deterministic pair -> pattern_analysis -------------------------
    # Two plain edges into one node is a join: LangGraph holds
    # ``pattern_analysis`` until *both* ``statistics`` and ``timeline`` have
    # written, so it always sees a complete pair rather than whichever
    # finished first.
    for node in _PATTERN_ANALYSIS_INPUTS:
        builder.add_edge(node, "pattern_analysis")

    # -- fan-in -------------------------------------------------------------
    # Every analysis stage feeds ``recommendation`` directly, including the
    # two that also feed ``pattern_analysis`` — it needs their raw artifacts,
    # not just the patterns derived from them.
    for node in _DIRECT_TO_RECOMMENDATION:
        builder.add_edge(node, "recommendation")

    # ``parser`` joins that fan-in too, because ``parser_metrics`` reaches
    # ``recommendation`` no other way: every analysis stage publishes its own
    # artifact rather than forwarding its inputs. The edge is what lets the
    # synthesis qualify its confidence by how much of the payload was readable.
    builder.add_edge("parser", "recommendation")

    # -- the optional web-search detour -------------------------------------
    # Only ``error_analysis`` routes conditionally, and only it may loop. The
    # path map is explicit so the two destinations show up in a rendered graph
    # rather than being inferred at runtime.
    builder.add_conditional_edges(
        "error_analysis",
        route_after_error_analysis,
        {"web_search": "web_search", "recommendation": "recommendation"},
    )
    builder.add_edge("web_search", "error_analysis")

    # -- deterministic epilogue --------------------------------------------
    builder.add_edge("recommendation", "report_generator")
    builder.add_edge("report_generator", END)

    return builder


def compile_graph() -> CompiledStateGraph:
    """Build and compile the LogSherlock graph.

    Returns a runnable ``CompiledStateGraph``. A checkpointer is intentionally
    omitted: LogSherlock treats each investigation as a stateless request and
    receives prior context via ``historical_context`` in the input state.
    """
    return build_graph().compile()


__all__ = [
    "LogSherlockState",
    "route_after_error_analysis",
    "build_graph",
    "compile_graph",
]


"""LogSherlock — multi-agent log analysis graph.

This module defines *only* the graph architecture for LogSherlock:

    * the graph state (input / working / output),
    * the reducers used to merge concurrent updates,
    * placeholder node definitions,
    * the fixed graph topology,
    * a ``compile_graph()`` factory.

No business logic lives here: implemented nodes are built in their own feature
packages (``parser/``, ``stats/``, ``timeline/``, ``error_analysis/``) and
merely registered below.
Every node that is not implemented yet is a deterministic stub that documents
its future responsibility via a ``TODO`` block and returns an empty state
delta. Prompts, LLM calls, recommendation logic and report rendering are
intentionally left unimplemented.

Topology (fixed workflow)::

    START
      -> coordinator            (deterministic)
      -> parser                 (deterministic)
      -> [ error_analysis       (LLM, parallel)  <-+
           pattern_analysis     (LLM, parallel)    | optional, opt-in
           statistics           (deterministic)    |
           timeline ]           (deterministic)    |
             |                                     |
             +-> web_search     (network) ---------+
      -> recommendation         (LLM)
      -> report_generator       (LLM)
      -> END

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

# Shared graph models live in the dedicated ``models`` package — the single
# source of truth for every structure that crosses a node boundary. Feature
# packages (parser, statistics, ...) import from here; they never redefine.
from models import (
    AnalysisMode,
    ErrorSummary,
    LLMProvider,
    ParsedLogEntry,
    ParserMetrics,
    Statistics,
    TimelineEvent,
)

# The error_analysis, parser, statistics, timeline and web_search nodes are
# implemented as standalone feature packages (see ``error_analysis/``,
# ``parser/``, ``stats/``, ``timeline/`` and ``web_search/``). They are imported
# here and registered directly in ``build_graph`` — this module defines no stub
# for them.
from error_analysis import error_analysis_node
from parser import parser_node
from stats import statistics_node
from timeline import timeline_node
from web_search.node import web_search_node

# ---------------------------------------------------------------------------
# Payload type aliases
# ---------------------------------------------------------------------------
# Concrete models (``ParsedLogEntry``, ``ParserMetrics``, ``Statistics``,
# ``TimelineEvent``, ``ErrorSummary``) now live in the shared ``models``
# package. The aliases below remain deliberately loose (``dict[str, Any]``)
# placeholders for payloads whose nodes are not yet implemented; each should
# graduate into a ``models`` module (``PatternSummary``,
# ``HistoricalInvestigation``, ...) as that node lands.

PatternSummary = dict[str, Any]
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

    # The four fan-out nodes each own exactly one of the fields below and are
    # the *sole* writer of that field within the parallel superstep. Because
    # there is no write contention on any single key, these fields need NO
    # reducer — LangGraph merges disjoint keys from concurrent branches
    # automatically. (See ``investigation_notes`` / ``completed_stages`` for
    # the fields that genuinely require a reducer.)
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


def coordinator_node(state: LogSherlockState) -> LogSherlockState:
    """Deterministic entrypoint that validates and normalizes the request.

    TODO:
        * Validate required INPUT fields (application_name, raw_logs, ...).
        * Default ``analysis_mode`` to ``"standard"`` when omitted.
        * Normalize / clamp ``investigation_timestamp``.
        * Reject empty or oversized log payloads early with a clear error.
        * Emit an ``investigation_notes`` entry for any recoverable input issue.
    """
    return {"completed_stages": ["coordinator"]}


def pattern_analysis_node(state: LogSherlockState) -> LogSherlockState:
    """LLM agent that detects behavioral patterns (parallel branch).

    TODO:
        * Prompt an LLM to surface recurring sequences, spikes and anomalies.
        * Correlate patterns across services / loggers.
        * Produce a ``PatternSummary`` describing notable patterns.
    """
    return {"pattern_summary": {}, "completed_stages": ["pattern_analysis"]}


def recommendation_node(state: LogSherlockState) -> LogSherlockState:
    """LLM agent that synthesizes findings into a root cause + recommendation.

    This is the fan-in point: it reads every WORKING artifact produced by the
    four parallel branches and compares them against ``historical_context``.

    TODO:
        * Fuse error_summary, pattern_summary, statistics and timeline.
        * Compare current signals against prior investigations (regressions,
          recurring issues, drift) from ``historical_context``.
        * Infer the most likely ``root_cause`` and a ``confidence_score``.
        * Draft the ``executive_summary``.
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

# Nodes that fan out from the parser and fan back in to the recommendation
# agent. Declared once so the topology stays readable and DRY.
_PARALLEL_ANALYSIS_NODES: tuple[str, ...] = (
    "error_analysis",
    "pattern_analysis",
    "statistics",
    "timeline",
)

# The parallel branches whose route to ``recommendation`` is a plain edge.
# ``error_analysis`` is missing because it routes conditionally — see
# ``route_after_error_analysis``.
_DIRECT_TO_RECOMMENDATION: tuple[str, ...] = tuple(
    node for node in _PARALLEL_ANALYSIS_NODES if node != "error_analysis"
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
    builder.add_node("coordinator", coordinator_node)
    builder.add_node("parser", parser_node)
    builder.add_node("error_analysis", error_analysis_node)
    builder.add_node("web_search", web_search_node)
    builder.add_node("pattern_analysis", pattern_analysis_node)
    builder.add_node("statistics", statistics_node)
    builder.add_node("timeline", timeline_node)
    # ``defer`` holds this node back until no other task is pending, which is
    # what keeps the fan-in a fan-in now that one branch can take a detour.
    # Without it the join fires as soon as the three plain edges below have
    # been written — LangGraph treats the conditional branch out of
    # ``error_analysis`` as a separate trigger rather than a fourth member of
    # the join — and ``recommendation`` runs twice: once on an incomplete
    # state, then again after the web search lands.
    builder.add_node("recommendation", recommendation_node, defer=True)
    builder.add_node("report_generator", report_generator_node)

    # -- deterministic preamble --------------------------------------------
    builder.add_edge(START, "coordinator")
    builder.add_edge("coordinator", "parser")

    # -- fan-out / fan-in ---------------------------------------------------
    # parser -> each analysis node (fan-out into a single parallel superstep),
    # then each analysis node -> recommendation (fan-in). LangGraph will not
    # start ``recommendation`` until *all* four branches complete.
    for node in _PARALLEL_ANALYSIS_NODES:
        builder.add_edge("parser", node)
    for node in _DIRECT_TO_RECOMMENDATION:
        builder.add_edge(node, "recommendation")

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


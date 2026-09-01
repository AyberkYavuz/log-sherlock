"""Tests for the graph topology itself — the wiring, not the nodes.

The node suites all assert what a node *computes*. This one asserts what the
graph *is*, because the topology is a contract every node depends on and a
wrong edge fails silently: a stage still runs, just on a state that is missing
the artifact it was supposed to read.

What is pinned here:

    * the exact node set, so a removed node (``coordinator``) cannot creep back
      in and a new one cannot land undocumented;
    * the exact edge set, since ``pattern_analysis`` reading the deterministic
      pair, ``prepare_output`` reading all four stages, and ``prepare_output``
      reading ``parser_metrics`` straight from ``parser`` are all invisible in
      the output when miswired;
    * acyclicity, with the one bounded ``error_analysis <-> web_search`` loop
      named as the sole documented exception;
    * the ordering the edges imply, observed through a real run.
"""

from __future__ import annotations

from graph import compile_graph

#: Every node in the compiled graph, LangGraph's ``__start__`` / ``__end__``
#: sentinels included.
EXPECTED_NODES = {
    "__start__",
    "parser",
    "error_analysis",
    "web_search",
    "statistics",
    "timeline",
    "pattern_analysis",
    "prepare_output",
    "write_to_db",
    "__end__",
}

#: Every edge, as ``(source, target)``. Conditional and plain edges alike —
#: what matters to a reader is which state reaches which node.
EXPECTED_EDGES = {
    ("__start__", "parser"),
    # parser fans out into three parallel branches
    ("parser", "error_analysis"),
    ("parser", "statistics"),
    ("parser", "timeline"),
    # the bounded, opt-in detour
    ("error_analysis", "web_search"),
    ("web_search", "error_analysis"),
    # pattern_analysis runs downstream of both deterministic nodes
    ("statistics", "pattern_analysis"),
    ("timeline", "pattern_analysis"),
    # every analysis stage fans in to prepare_output, and so does the parser —
    # ``parser_metrics`` reaches the synthesis no other way
    ("error_analysis", "prepare_output"),
    ("statistics", "prepare_output"),
    ("timeline", "prepare_output"),
    ("pattern_analysis", "prepare_output"),
    ("parser", "prepare_output"),
    ("prepare_output", "write_to_db"),
    ("write_to_db", "__end__"),
}

#: Pinned separately from the set above so a *duplicate* edge, or an extra one
#: that happens to be spelled like an existing member, cannot slip through on
#: set semantics alone.
EXPECTED_EDGE_COUNT = 15

#: The only cycle the topology permits, bounded to a single lap by
#: ``route_after_error_analysis``.
DOCUMENTED_CYCLE = {("error_analysis", "web_search"), ("web_search", "error_analysis")}

RAW_LOGS = "\n".join(
    [
        "2026-01-01T00:00:00Z ERROR order-service payment provider unreachable",
        "2026-01-01T00:00:01Z INFO order-service retrying",
        "2026-01-01T00:00:02Z ERROR order-service order 41 failed",
    ]
)


def _graph() -> tuple[set[str], set[tuple[str, str]]]:
    drawable = compile_graph().get_graph()
    return (
        set(drawable.nodes),
        {(edge.source, edge.target) for edge in drawable.edges},
    )


def test_the_node_set_is_exactly_what_the_topology_documents() -> None:
    nodes, _ = _graph()
    assert nodes == EXPECTED_NODES


def test_the_obsolete_coordinator_node_is_gone() -> None:
    # Called out separately from the node-set assertion because this is the one
    # absence a reader is likely to be checking for deliberately.
    nodes, edges = _graph()
    assert "coordinator" not in nodes
    assert not any("coordinator" in (source, target) for source, target in edges)


def test_the_edge_set_is_exactly_what_the_topology_documents() -> None:
    _, edges = _graph()
    assert edges == EXPECTED_EDGES


def test_the_edge_count_matches_and_no_edge_is_declared_twice() -> None:
    # Read as a list rather than through ``_graph``: the set comparison above
    # cannot tell one ``parser -> prepare_output`` edge from two.
    declared = [(edge.source, edge.target) for edge in compile_graph().get_graph().edges]

    assert len(declared) == EXPECTED_EDGE_COUNT
    assert len(set(declared)) == len(declared)


def test_prepare_output_reads_the_parser_directly() -> None:
    # ``parser_metrics`` reaches the synthesis through this edge and no other:
    # every analysis stage publishes its own artifact rather than forwarding
    # its inputs. Pinned on its own so the reason survives a future edit to
    # the edge list.
    _, edges = _graph()
    assert ("parser", "prepare_output") in edges


def test_no_node_is_orphaned() -> None:
    nodes, edges = _graph()
    sources = {source for source, _ in edges}
    targets = {target for _, target in edges}
    # Every node is reachable and every node leads somewhere, the two sentinels
    # excepted — ``__start__`` has no inbound edge and ``__end__`` no outbound.
    assert nodes - targets == {"__start__"}
    assert nodes - sources == {"__end__"}


def test_the_web_search_loop_is_the_only_cycle() -> None:
    _, edges = _graph()
    acyclic = edges - DOCUMENTED_CYCLE

    successors: dict[str, set[str]] = {}
    for source, target in acyclic:
        successors.setdefault(source, set()).add(target)

    # Iterative depth-first walk with an on-stack marker; a node seen twice on
    # the same path is a cycle.
    visited: set[str] = set()

    def walk(start: str) -> None:
        stack = [(start, iter(successors.get(start, ())))]
        on_path = {start}
        while stack:
            node, children = stack[-1]
            following = next(children, None)
            if following is None:
                stack.pop()
                on_path.discard(node)
                visited.add(node)
                continue
            assert following not in on_path, f"cycle through {following}"
            if following not in visited:
                on_path.add(following)
                stack.append((following, iter(successors.get(following, ()))))

    for node in successors:
        if node not in visited:
            walk(node)


def test_execution_order_follows_the_documented_dependencies() -> None:
    # A real run rather than a graph inspection, because the thing at stake is
    # scheduling: ``pattern_analysis`` reads what ``statistics`` and
    # ``timeline`` wrote, so it has to run after *both*, and it has to run
    # before the stage that consumes it.
    #
    # ``error_analysis`` runs without a provider configured and degrades to its
    # deterministic findings — this test is about ordering, not analysis
    # quality, so that path is fine here.
    final = compile_graph().invoke({"application_name": "orders", "raw_logs": RAW_LOGS})
    stages = final["completed_stages"]

    # Each stage runs exactly once — the fan-in joins do not double-fire.
    for stage in EXPECTED_NODES - {"__start__", "__end__", "web_search"}:
        assert stages.count(stage) == 1, stages

    at = {stage: stages.index(stage) for stage in set(stages)}
    assert at["parser"] < at["error_analysis"]
    assert at["parser"] < at["statistics"]
    assert at["parser"] < at["timeline"]
    # The direct edge must not pull the fan-in forward: ``defer`` still holds
    # ``prepare_output`` until every branch has landed, so the earliest
    # contributor to its join does not get to start it.
    assert at["parser"] < at["prepare_output"]
    assert at["statistics"] < at["pattern_analysis"]
    assert at["timeline"] < at["pattern_analysis"]
    assert at["pattern_analysis"] < at["prepare_output"]
    assert at["error_analysis"] < at["prepare_output"]
    assert at["prepare_output"] < at["write_to_db"]

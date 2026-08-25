"""The deterministic Statistics Node for the LogSherlock graph.

The node answers one question about the parser's output — *"what does the
parsed dataset contain?"* — and answers it with facts only: no LLM, no prompts,
no network, no interpretation. Given the same ``parsed_logs`` it always returns
the same :class:`~graph_library.models.statistics.Statistics` payload.

Scope boundaries it deliberately respects:

    * **Parser health** (total / parsed / malformed line counts) belongs to
      ``parser_metrics``; it is never recomputed or mirrored here — downstream
      nodes read it straight from that field.
    * **Temporal behaviour** (time buckets, activity over time, spikes, error
      onset and recovery) belongs to the timeline node. Statistics reports only
      dataset-level timestamp coverage: how many records are timestamped, and
      the earliest / latest event time.

Internally the aggregation runs on a pandas ``DataFrame`` (see
:mod:`graph_library.stats.aggregations`), but the payload that leaves this module is plain,
JSON-serializable Python — no DataFrame ever enters graph state.

The public entry point is :func:`statistics_node`, whose signature matches the
other graph nodes (full state in, partial state delta out).
"""

from __future__ import annotations

from typing import Any

from graph_library.models import ParsedLogEntry, Statistics

from .aggregations import (
    TOP_VALUE_LIMIT,
    build_frame,
    build_metadata_frame,
    distribution,
    metadata_distributions,
    severity_summary,
    timestamp_coverage,
)


def compute_statistics(parsed_logs: list[ParsedLogEntry]) -> Statistics:
    """Aggregate ``parsed_logs`` into the :class:`Statistics` payload.

    Args:
        parsed_logs: Normalized entries produced by the parser node. An empty
            list is valid and yields an empty-but-well-formed payload (zero
            counts, empty distributions, ``None`` timestamps) rather than an
            error or invented figures.

    Returns:
        A plain ``dict`` (``Statistics`` is a ``TypedDict``) containing only
        JSON-serializable values.
    """
    frame = build_frame(parsed_logs)
    metadata_frame = build_metadata_frame(parsed_logs)

    return Statistics(
        # ``level`` and ``logger`` are first-class: they are counted directly
        # from the normalized fields, capped at the dominant values for the UI,
        # and records missing the field are reported as a ``None`` value rather
        # than silently dropped.
        level_distribution=distribution(frame["level"], limit=TOP_VALUE_LIMIT),
        logger_distribution=distribution(frame["logger"], limit=TOP_VALUE_LIMIT),
        severity=severity_summary(frame["level"]),
        timestamp_coverage=timestamp_coverage(frame["timestamp"]),
        metadata_distributions=metadata_distributions(metadata_frame),
    )


def statistics_node(state: dict[str, Any]) -> dict[str, Any]:
    """Compute dataset statistics from ``parsed_logs``.

    Args:
        state: The LogSherlock graph state. Only ``parsed_logs`` is read;
            ``parser_metrics`` is deliberately *not* consumed — parser health is
            already structured and is consumed directly by downstream nodes.

    Returns:
        A partial state delta containing exactly:

            * ``statistics`` — the :class:`Statistics` payload,
            * ``completed_stages`` — ``["statistics"]``.

        No other state field is touched. ``completed_stages`` uses an additive
        reducer in the graph, so returning only this node's contribution is
        correct even though three sibling nodes run in the same superstep.
    """
    parsed_logs = state.get("parsed_logs") or []

    return {
        "statistics": compute_statistics(parsed_logs),
        "completed_stages": ["statistics"],
    }

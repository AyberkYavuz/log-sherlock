"""LogSherlock deterministic statistics feature package.

Contains only business logic — the shared model it produces
(:class:`~graph_library.models.Statistics`) lives in the shared ``graph_library.models`` package and is
imported from there.

The package is called ``stats`` rather than ``statistics`` on purpose: the repo
root is on ``sys.path``, so a top-level ``statistics`` package would shadow the
standard library module of that name for the whole process (pandas and its
dependency tree included). The graph node it registers is still named
``"statistics"``.

Public surface:

    * :func:`statistics_node` — the graph node entry point.
    * :func:`compute_statistics` and the aggregation helpers — for reuse and
      testing.
"""

from __future__ import annotations

from .aggregations import (
    ERROR_LEVELS,
    MAX_METADATA_CARDINALITY,
    TOP_VALUE_LIMIT,
    WARNING_LEVELS,
    build_frame,
    build_metadata_frame,
    distribution,
    metadata_distributions,
    severity_summary,
    timestamp_coverage,
)
from .statistics_node import compute_statistics, statistics_node

__all__ = [
    "statistics_node",
    "compute_statistics",
    "build_frame",
    "build_metadata_frame",
    "distribution",
    "metadata_distributions",
    "severity_summary",
    "timestamp_coverage",
    "ERROR_LEVELS",
    "WARNING_LEVELS",
    "TOP_VALUE_LIMIT",
    "MAX_METADATA_CARDINALITY",
]

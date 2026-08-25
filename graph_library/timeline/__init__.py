"""LogSherlock deterministic timeline feature package.

Contains only business logic — the shared model it produces
(:class:`~graph_library.models.TimelineEvent`) lives in the shared ``graph_library.models`` package and is
imported from there.

The package is laid out by concern:

    * :mod:`graph_library.timeline.granularity` — adaptive bucket sizing and time arithmetic,
    * :mod:`graph_library.timeline.buckets` — bucket construction, aggregation and rendering,
    * :mod:`graph_library.timeline.milestones` — inflection-point detection,
    * :mod:`graph_library.timeline.node` — the graph node that ties the three together.

Public surface:

    * :func:`timeline_node` — the graph node entry point.
    * :func:`build_timeline` and the helpers below — for reuse and testing.
"""

from __future__ import annotations

from .buckets import (
    ERROR_LEVELS,
    SAMPLE_MESSAGE_LIMIT,
    SAMPLE_MESSAGE_MAX_LENGTH,
    TOP_LOGGER_LIMIT,
    WARNING_LEVELS,
    TimeBucket,
    bucket_event,
    build_buckets,
    is_error,
    is_warning,
    sample_messages,
    timestamped_entries,
    top_loggers,
)
from .granularity import (
    COARSE_BUCKET,
    FINE_BUCKET,
    MEDIUM_BUCKET,
    WIDE_BUCKET,
    bucket_index,
    describe_duration,
    floor_to_bucket,
    select_bucket_size,
    to_comparable,
)
from .milestones import ErrorNarrative, detect_milestones, resolve_narrative
from .node import NO_TIMESTAMPS_NOTE, build_timeline, timeline_node

__all__ = [
    "timeline_node",
    "build_timeline",
    "NO_TIMESTAMPS_NOTE",
    # granularity
    "select_bucket_size",
    "describe_duration",
    "floor_to_bucket",
    "bucket_index",
    "to_comparable",
    "FINE_BUCKET",
    "MEDIUM_BUCKET",
    "COARSE_BUCKET",
    "WIDE_BUCKET",
    # buckets
    "TimeBucket",
    "timestamped_entries",
    "build_buckets",
    "bucket_event",
    "top_loggers",
    "sample_messages",
    "is_error",
    "is_warning",
    "ERROR_LEVELS",
    "WARNING_LEVELS",
    "TOP_LOGGER_LIMIT",
    "SAMPLE_MESSAGE_LIMIT",
    "SAMPLE_MESSAGE_MAX_LENGTH",
    # milestones
    "detect_milestones",
    "resolve_narrative",
    "ErrorNarrative",
]

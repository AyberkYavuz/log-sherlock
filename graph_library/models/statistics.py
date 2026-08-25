"""The deterministic dataset-composition model produced by the Statistics Node.

``Statistics`` answers exactly one question — *"what does the parsed dataset
contain?"* — as plain, JSON-serializable facts. It deliberately does **not**
contain:

    * parser health (that is :class:`~graph_library.models.parser_metrics.ParserMetrics`;
      notably ``parsed_lines`` is never duplicated here),
    * temporal behaviour (time buckets, spikes, error onset/recovery — those
      belong to the timeline node),
    * any semantic interpretation (that is the LLM analysis nodes' job).

Like every other shared model this is a :class:`~typing.TypedDict`, so a
``Statistics`` value *is* the plain dict that flows through LangGraph state:
no serialization layer, no pandas objects, one representation everywhere.
"""

from __future__ import annotations

from typing import Any, TypedDict


class CategoryCount(TypedDict):
    """One row of a categorical distribution.

    A list of these (rather than a ``{value: count}`` mapping) is the wire
    format for every distribution because it makes the ordering an explicit,
    reviewable part of the payload and keeps non-string values (ints, floats,
    booleans, ``None``) representable without stringifying them.

    Attributes:
        value: The observed value. ``None`` means the field was absent for
            those records (only ``level`` / ``logger`` distributions use it —
            metadata distributions exclude missing values entirely).
        count: How many records carried that value.
    """

    value: Any  # str | int | float | bool | None
    count: int


class SeveritySummary(TypedDict):
    """Deterministic severity counts derived from the normalized ``level``.

    Only spelling variants of the same level are folded together (``WARN`` and
    ``WARNING``); no semantic reclassification happens — ``FATAL``, ``CRITICAL``
    and friends stay visible in ``Statistics.level_distribution`` instead of
    being silently promoted into ``error_count``.

    Attributes:
        error_count: Records whose level is an ERROR spelling.
        warning_count: Records whose level is a WARNING spelling.
        error_ratio: ``error_count`` over *all* records (records without a
            level included in the denominator), rounded to 4 decimals. ``0.0``
            for an empty dataset.
        warning_ratio: Same, for ``warning_count``.
    """

    error_count: int
    warning_count: int
    error_ratio: float
    warning_ratio: float


class TimestampCoverage(TypedDict):
    """Dataset-level timestamp facts — *not* temporal analysis.

    Timestamps are consumed exactly as the parser normalized them; Statistics
    never re-parses them.

    Attributes:
        with_timestamp: Records carrying a usable timestamp.
        without_timestamp: Records whose timestamp is ``None``/unusable.
        earliest: ISO-8601 string of the earliest timestamp, or ``None``.
        latest: ISO-8601 string of the latest timestamp, or ``None``.
    """

    with_timestamp: int
    without_timestamp: int
    earliest: str | None
    latest: str | None


class Statistics(TypedDict):
    """Aggregate, deterministic facts about ``parsed_logs``.

    Attributes:
        level_distribution: Counts per observed ``level``, most frequent first,
            capped at the top 20 values. Records without a level appear as a
            ``None`` value.
        logger_distribution: Same, for ``logger``.
        severity: Error / warning counts and ratios.
        timestamp_coverage: Dataset-level timestamp facts.
        metadata_distributions: Distribution per *dynamically discovered*
            metadata key, keyed by metadata key in alphabetical order. Only
            low-cardinality keys (at most 21 distinct meaningful values) with
            aggregatable scalar values appear; every other key is omitted
            entirely rather than partially reported.
    """

    level_distribution: list[CategoryCount]
    logger_distribution: list[CategoryCount]
    severity: SeveritySummary
    timestamp_coverage: TimestampCoverage
    metadata_distributions: dict[str, list[CategoryCount]]

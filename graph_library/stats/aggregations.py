"""Deterministic pandas aggregations behind the Statistics Node.

Every function here takes plain Python (``ParsedLogEntry`` dicts), does its work
on a :class:`pandas.DataFrame`, and returns plain Python again — no pandas
object ever escapes this module, so nothing pandas-shaped can reach LangGraph
state.

Two rules govern the whole module:

    * **Determinism.** Identical ``parsed_logs`` always yield an identical
      result, including ordering. Distributions are sorted by descending count
      with an explicit ``(str(value), type name)`` tiebreaker; metadata keys are
      emitted in alphabetical order. Nothing depends on dict/set iteration.
    * **Ecosystem independence.** No metadata field name is ever hard-coded;
      keys are discovered from the records themselves.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from graph_library.models import CategoryCount, ParsedLogEntry, SeveritySummary, TimestampCoverage

# -- product / UI rules ------------------------------------------------------

#: How many values a first-class distribution (level, logger) may return. The
#: UI renders the dominant values only.
TOP_VALUE_LIMIT = 20

#: Highest number of distinct meaningful values a metadata key may have and
#: still be considered *low cardinality* (0–21 low, 22+ high). A high-cardinality
#: key is omitted from the output entirely — a 500-bar chart helps nobody.
MAX_METADATA_CARDINALITY = 21

# -- severity vocabulary -----------------------------------------------------
# Spelling variants only. These are the same level written differently by
# different ecosystems (Spring Boot emits WARN, Python emits WARNING); folding
# them together is normalization, not semantic classification. Levels such as
# FATAL / CRITICAL are deliberately absent — they are distinct levels and stay
# visible in the level distribution instead of being folded into errors.
ERROR_LEVELS: frozenset[str] = frozenset({"ERROR", "ERR"})
WARNING_LEVELS: frozenset[str] = frozenset({"WARN", "WARNING"})

#: Value types a metadata distribution can be built from. Anything else (dicts,
#: lists, arbitrary objects) is not categorical data and disqualifies its key.
_SCALAR_TYPES = (str, bool, int, float)


# ---------------------------------------------------------------------------
# Frame construction
# ---------------------------------------------------------------------------


def build_frame(parsed_logs: list[ParsedLogEntry]) -> pd.DataFrame:
    """Build the first-class-fields frame (``level``, ``logger``, ``timestamp``).

    Columns are forced to ``object`` dtype so values survive exactly as the
    parser produced them: ``None`` stays ``None`` (rather than becoming ``NaN``
    or an empty string) and timezone-aware and naive datetimes can coexist.
    """
    return pd.DataFrame(
        {
            "level": pd.Series(
                [entry.get("level") for entry in parsed_logs], dtype=object
            ),
            "logger": pd.Series(
                [entry.get("logger") for entry in parsed_logs], dtype=object
            ),
            "timestamp": pd.Series(
                [entry.get("timestamp") for entry in parsed_logs], dtype=object
            ),
        }
    )


def build_metadata_frame(parsed_logs: list[ParsedLogEntry]) -> pd.DataFrame:
    """Build the dynamic metadata frame — one column per discovered key.

    Keys are discovered from the records; records that lack a key simply have a
    missing cell there. Non-dict / empty ``metadata`` contributes nothing.
    """
    records = [
        entry.get("metadata")
        for entry in parsed_logs
        if isinstance(entry.get("metadata"), dict) and entry["metadata"]
    ]
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records, dtype=object)


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------


def distribution(
    series: pd.Series,
    *,
    limit: int | None = None,
    include_missing: bool = True,
) -> list[CategoryCount]:
    """Count values in ``series`` and return them in deterministic order.

    Args:
        series: An ``object``-dtype column.
        limit: Keep at most this many rows (the most frequent ones).
        include_missing: When true, missing values are reported as a single
            ``value: None`` row; when false they are dropped.

    Returns:
        Rows sorted by descending count, ties broken by the value's string form
        and then its type name so the order never depends on hash iteration.
    """
    counts = series.value_counts(dropna=not include_missing)
    rows = [
        CategoryCount(value=_native(value), count=int(count))
        for value, count in counts.items()
    ]
    rows.sort(key=lambda row: (-row["count"], _tiebreak(row["value"])))
    return rows if limit is None else rows[:limit]


def metadata_distributions(frame: pd.DataFrame) -> dict[str, list[CategoryCount]]:
    """Build a distribution for every *eligible* metadata key.

    A key is eligible when all of the following hold; otherwise it is omitted
    from the result entirely (no partial distribution, no "high cardinality"
    marker — the output only ever contains directly renderable facts):

        * at least one record carries a meaningful (non-missing) value,
        * every meaningful value is a scalar (``str``/``bool``/``int``/``float``);
          a single nested dict or list disqualifies the key rather than
          producing a misleading partial count,
        * the number of distinct meaningful values is at most
          :data:`MAX_METADATA_CARDINALITY`.

    Missing values never appear as a category and never count toward
    cardinality, so a mostly-absent key is judged on the values it actually has.
    """
    result: dict[str, list[CategoryCount]] = {}
    for key in sorted(name for name in frame.columns if isinstance(name, str)):
        values = frame[key].dropna()
        if values.empty or not values.map(_is_scalar).all():
            continue
        counts = distribution(values, include_missing=False)
        if not counts or len(counts) > MAX_METADATA_CARDINALITY:
            continue
        result[key] = counts
    return result


# ---------------------------------------------------------------------------
# Severity + timestamps
# ---------------------------------------------------------------------------


def severity_summary(levels: pd.Series) -> SeveritySummary:
    """Count error / warning records and express them as ratios.

    The ratio denominator is the *whole* dataset (records without a level
    included), so the numbers read as "share of all records", which is what a
    reader assumes when they see a percentage next to a record set.
    """
    total = int(len(levels))
    errors = int(levels.isin(ERROR_LEVELS).sum())
    warnings = int(levels.isin(WARNING_LEVELS).sum())
    return SeveritySummary(
        error_count=errors,
        warning_count=warnings,
        error_ratio=_ratio(errors, total),
        warning_ratio=_ratio(warnings, total),
    )


def timestamp_coverage(timestamps: pd.Series) -> TimestampCoverage:
    """Summarize how much of the dataset is timestamped, and its span.

    Timestamps are *not* re-parsed: the parser already normalized them. A
    UTC-normalized copy is used purely as a sort key so naive and aware
    datetimes can be compared without raising, while the reported earliest /
    latest values are the original datetimes, unmodified.
    """
    total = int(len(timestamps))
    sort_key = pd.to_datetime(timestamps, utc=True, errors="coerce")
    usable = int(sort_key.notna().sum())
    earliest = latest = None
    if usable:
        earliest = _isoformat(timestamps.loc[sort_key.idxmin()])
        latest = _isoformat(timestamps.loc[sort_key.idxmax()])
    return TimestampCoverage(
        with_timestamp=usable,
        without_timestamp=total - usable,
        earliest=earliest,
        latest=latest,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_missing(value: Any) -> bool:
    """Return whether ``value`` carries no information.

    Written by hand rather than via ``pd.isna`` because ``pd.isna`` returns an
    *array* for list-like input, which would raise when used as a condition —
    and metadata values may well be lists.
    """
    if value is None or value is pd.NaT:
        return True
    return isinstance(value, float) and value != value  # NaN is not equal to itself


def _is_scalar(value: Any) -> bool:
    """Return whether ``value`` can take part in a categorical distribution."""
    return isinstance(value, _SCALAR_TYPES) and not _is_missing(value)


def _native(value: Any) -> Any:
    """Convert a pandas/numpy scalar into a plain, JSON-serializable value."""
    if _is_missing(value):
        return None
    if isinstance(value, _SCALAR_TYPES):
        return value
    item = getattr(value, "item", None)  # numpy scalar -> Python scalar
    return item() if callable(item) else value


def _tiebreak(value: Any) -> tuple[str, str]:
    """Deterministic secondary sort key for values of mixed, unorderable types."""
    return str(value), type(value).__name__


def _ratio(part: int, total: int) -> float:
    """Return ``part / total`` rounded for display, or ``0.0`` when empty."""
    return round(part / total, 4) if total else 0.0


def _isoformat(value: Any) -> str | None:
    """Render a timestamp as ISO-8601, keeping the payload JSON-serializable."""
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else None

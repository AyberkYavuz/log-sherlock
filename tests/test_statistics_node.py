"""Unit tests for the deterministic Statistics Node (``graph_library/stats/``).

Entries are built with :func:`_entry`, which mirrors the ``ParsedLogEntry``
schema the parser emits, so these tests exercise the node against exactly the
shape it receives in the graph.

Documented conventions asserted here:

    * a missing ``level`` / ``logger`` is reported as a ``value: None`` row —
      one consistent representation, never an invented ``"UNKNOWN"`` string;
    * a missing metadata value is *excluded* from its distribution and does not
      count toward cardinality;
    * a metadata key is included only when it has 1..21 distinct meaningful
      scalar values — otherwise it is omitted from the payload entirely.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from graph import compile_graph
from graph_library.models import ParsedLogEntry
from graph_library.parser.parser_node import parser_node
from graph_library.stats import MAX_METADATA_CARDINALITY, TOP_VALUE_LIMIT, compute_statistics
from graph_library.stats.statistics_node import statistics_node

_SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_logs"

EXPECTED_KEYS = {
    "level_distribution",
    "logger_distribution",
    "severity",
    "timestamp_coverage",
    "metadata_distributions",
}


def _entry(
    line_number: int = 1,
    *,
    level: str | None = None,
    logger: str | None = None,
    timestamp: datetime | None = None,
    message: str = "msg",
    metadata: dict[str, Any] | None = None,
) -> ParsedLogEntry:
    """Build a normalized entry exactly as the parser would emit it."""
    return ParsedLogEntry(
        line_number=line_number,
        raw=message,
        timestamp=timestamp,
        level=level,
        logger=logger,
        message=message,
        metadata=metadata if metadata is not None else {},
    )


def _values(rows: list[dict]) -> list[Any]:
    return [row["value"] for row in rows]


def _as_mapping(rows: list[dict]) -> dict[Any, int]:
    return {row["value"]: row["count"] for row in rows}


# ---------------------------------------------------------------------------
# Node contract
# ---------------------------------------------------------------------------


def test_return_shape_and_completed_stage() -> None:
    result = statistics_node({"parsed_logs": [_entry(level="INFO")]})
    assert set(result) == {"statistics", "completed_stages"}
    assert result["completed_stages"] == ["statistics"]
    assert set(result["statistics"]) == EXPECTED_KEYS


def test_does_not_touch_other_state_fields() -> None:
    result = statistics_node(
        {"parsed_logs": [_entry(level="INFO")], "application_name": "svc"}
    )
    assert "application_name" not in result
    assert "parser_metrics" not in result


def test_missing_parsed_logs_key_is_safe() -> None:
    result = statistics_node({})
    assert result["statistics"]["level_distribution"] == []


def test_parser_metrics_are_not_duplicated() -> None:
    # parsed_lines (and every other parser-health figure) is owned by
    # ParserMetrics; Statistics must not mirror it under any name.
    statistics = compute_statistics([_entry(level="INFO"), _entry(2, level="ERROR")])
    flat = json.dumps(statistics)
    for forbidden in ("parsed_lines", "total_lines", "malformed", "blank_lines"):
        assert forbidden not in flat
    assert "record_count" not in statistics
    assert "total" not in statistics


def test_payload_is_json_serializable_and_pandas_free() -> None:
    entries = [
        _entry(1, level="INFO", logger="app", timestamp=datetime(2026, 1, 1)),
        _entry(2, level="ERROR", metadata={"status_code": 500, "ok": False, "r": 1.5}),
    ]
    statistics = compute_statistics(entries)
    # Round-trips through JSON unchanged: no numpy scalars, no Timestamps.
    assert json.loads(json.dumps(statistics)) == statistics


# ---------------------------------------------------------------------------
# Level distribution
# ---------------------------------------------------------------------------


def test_level_distribution_counts() -> None:
    entries = [
        _entry(1, level="INFO"),
        _entry(2, level="INFO"),
        _entry(3, level="ERROR"),
    ]
    assert compute_statistics(entries)["level_distribution"] == [
        {"value": "INFO", "count": 2},
        {"value": "ERROR", "count": 1},
    ]


def test_level_distribution_reports_missing_as_none() -> None:
    entries = [_entry(1, level="INFO"), _entry(2), _entry(3)]
    rows = compute_statistics(entries)["level_distribution"]
    assert _as_mapping(rows) == {None: 2, "INFO": 1}


def test_level_distribution_preserves_non_standard_levels() -> None:
    # No assumption that levels come from a fixed vocabulary.
    entries = [_entry(1, level="NOTICE"), _entry(2, level="AUDIT"), _entry(3, level="7")]
    assert set(_values(compute_statistics(entries)["level_distribution"])) == {
        "NOTICE",
        "AUDIT",
        "7",
    }


def test_level_distribution_is_capped_at_top_20() -> None:
    # 25 distinct levels with strictly decreasing frequency: only the 20 most
    # frequent survive, in order.
    entries: list[ParsedLogEntry] = []
    line = 0
    for rank in range(25):
        for _ in range(25 - rank):
            line += 1
            entries.append(_entry(line, level=f"LEVEL_{rank:02d}"))
    rows = compute_statistics(entries)["level_distribution"]
    assert len(rows) == TOP_VALUE_LIMIT == 20
    assert _values(rows) == [f"LEVEL_{rank:02d}" for rank in range(20)]


def test_level_distribution_returns_all_values_when_fewer_than_20() -> None:
    entries = [_entry(i, level=f"L{i}") for i in range(1, 6)]
    assert len(compute_statistics(entries)["level_distribution"]) == 5


# ---------------------------------------------------------------------------
# Logger distribution
# ---------------------------------------------------------------------------


def test_logger_distribution_counts_and_missing() -> None:
    entries = [
        _entry(1, logger="com.acme.Order"),
        _entry(2, logger="com.acme.Order"),
        _entry(3, logger="uvicorn.access"),
        _entry(4),
    ]
    rows = compute_statistics(entries)["logger_distribution"]
    assert rows == [
        {"value": "com.acme.Order", "count": 2},
        {"value": None, "count": 1},
        {"value": "uvicorn.access", "count": 1},
    ]


def test_logger_distribution_is_capped_at_top_20() -> None:
    entries: list[ParsedLogEntry] = []
    line = 0
    for rank in range(30):
        for _ in range(30 - rank):
            line += 1
            entries.append(_entry(line, logger=f"logger_{rank:02d}"))
    rows = compute_statistics(entries)["logger_distribution"]
    assert len(rows) == 20
    assert _values(rows)[0] == "logger_00"


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


def test_severity_counts_and_ratios() -> None:
    entries = [
        _entry(1, level="ERROR"),
        _entry(2, level="ERROR"),
        _entry(3, level="WARNING"),
        _entry(4, level="INFO"),
        _entry(5),
    ]
    assert compute_statistics(entries)["severity"] == {
        "error_count": 2,
        "warning_count": 1,
        "error_ratio": 0.4,  # denominator is every record, level-less included
        "warning_ratio": 0.2,
    }


def test_severity_folds_warn_and_warning_spellings() -> None:
    entries = [_entry(1, level="WARN"), _entry(2, level="WARNING"), _entry(3, level="ERR")]
    severity = compute_statistics(entries)["severity"]
    assert severity["warning_count"] == 2
    assert severity["error_count"] == 1


def test_severity_does_not_reclassify_other_levels() -> None:
    # FATAL / CRITICAL are distinct levels, not extra "error" categories: they
    # stay visible in the level distribution instead.
    entries = [_entry(1, level="FATAL"), _entry(2, level="CRITICAL"), _entry(3)]
    statistics = compute_statistics(entries)
    assert statistics["severity"]["error_count"] == 0
    assert set(_values(statistics["level_distribution"])) == {"FATAL", "CRITICAL", None}


def test_severity_with_no_levels_at_all() -> None:
    assert compute_statistics([_entry(1), _entry(2)])["severity"] == {
        "error_count": 0,
        "warning_count": 0,
        "error_ratio": 0.0,
        "warning_ratio": 0.0,
    }


# ---------------------------------------------------------------------------
# Timestamp coverage
# ---------------------------------------------------------------------------


def test_timestamp_coverage_all_present() -> None:
    base = datetime(2026, 8, 12, 10, 0, 0)
    entries = [_entry(i, timestamp=base + timedelta(minutes=i)) for i in range(1, 4)]
    assert compute_statistics(entries)["timestamp_coverage"] == {
        "with_timestamp": 3,
        "without_timestamp": 0,
        "earliest": "2026-08-12T10:01:00",
        "latest": "2026-08-12T10:03:00",
    }


def test_timestamp_coverage_some_missing() -> None:
    entries = [
        _entry(1, timestamp=datetime(2026, 8, 12, 9, 0)),
        _entry(2),
        _entry(3, timestamp=datetime(2026, 8, 12, 8, 0)),
    ]
    coverage = compute_statistics(entries)["timestamp_coverage"]
    assert coverage["with_timestamp"] == 2
    assert coverage["without_timestamp"] == 1
    assert coverage["earliest"] == "2026-08-12T08:00:00"
    assert coverage["latest"] == "2026-08-12T09:00:00"


def test_timestamp_coverage_all_missing() -> None:
    assert compute_statistics([_entry(1), _entry(2)])["timestamp_coverage"] == {
        "with_timestamp": 0,
        "without_timestamp": 2,
        "earliest": None,
        "latest": None,
    }


def test_timestamp_coverage_mixes_aware_and_naive_without_crashing() -> None:
    entries = [
        _entry(1, timestamp=datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)),
        _entry(2, timestamp=datetime(2026, 8, 12, 9, 0)),  # naive, treated as UTC
    ]
    coverage = compute_statistics(entries)["timestamp_coverage"]
    assert coverage["with_timestamp"] == 2
    # Reported values are the original datetimes, not UTC-rewritten copies.
    assert coverage["earliest"] == "2026-08-12T09:00:00"
    assert coverage["latest"] == "2026-08-12T12:00:00+00:00"


def test_timestamp_coverage_single_record() -> None:
    coverage = compute_statistics([_entry(1, timestamp=datetime(2026, 1, 1))])[
        "timestamp_coverage"
    ]
    assert coverage["earliest"] == coverage["latest"] == "2026-01-01T00:00:00"


def test_statistics_has_no_timeline_analysis() -> None:
    # Time bucketing, rates and onset/recovery belong to the timeline node.
    entries = [_entry(i, timestamp=datetime(2026, 1, 1, 0, i)) for i in range(5)]
    flat = json.dumps(compute_statistics(entries))
    for forbidden in ("bucket", "per_minute", "spike", "onset", "recovery", "trend"):
        assert forbidden not in flat


# ---------------------------------------------------------------------------
# Dynamic metadata
# ---------------------------------------------------------------------------


def test_metadata_keys_are_discovered_dynamically() -> None:
    entries = [
        _entry(1, metadata={"method": "GET", "status_code": 200}),
        _entry(2, metadata={"method": "POST", "status_code": 500}),
    ]
    distributions = compute_statistics(entries)["metadata_distributions"]
    assert set(distributions) == {"method", "status_code"}
    assert _as_mapping(distributions["method"]) == {"GET": 1, "POST": 1}
    assert _as_mapping(distributions["status_code"]) == {200: 1, 500: 1}


def test_metadata_low_cardinality_distribution() -> None:
    entries = [_entry(i, metadata={"method": "GET"}) for i in range(1, 121)]
    entries += [_entry(i, metadata={"method": "POST"}) for i in range(121, 151)]
    entries += [_entry(i, metadata={"method": "PUT"}) for i in range(151, 156)]
    distributions = compute_statistics(entries)["metadata_distributions"]
    assert distributions["method"] == [
        {"value": "GET", "count": 120},
        {"value": "POST", "count": 30},
        {"value": "PUT", "count": 5},
    ]


def test_metadata_exactly_21_unique_values_is_included() -> None:
    entries = [
        _entry(i, metadata={"shard": f"shard-{i:02d}"})
        for i in range(MAX_METADATA_CARDINALITY)
    ]
    distributions = compute_statistics(entries)["metadata_distributions"]
    assert MAX_METADATA_CARDINALITY == 21
    assert len(distributions["shard"]) == 21


def test_metadata_exactly_22_unique_values_is_omitted() -> None:
    entries = [_entry(i, metadata={"shard": f"shard-{i:02d}"}) for i in range(22)]
    assert compute_statistics(entries)["metadata_distributions"] == {}


def test_metadata_high_cardinality_key_is_omitted_entirely() -> None:
    # No partial distribution, no unique_count, no high_cardinality marker.
    entries = [
        _entry(i, metadata={"trace_id": f"trace-{i}", "service": "orders"})
        for i in range(200)
    ]
    distributions = compute_statistics(entries)["metadata_distributions"]
    assert set(distributions) == {"service"}
    assert "trace_id" not in json.dumps(distributions)


def test_metadata_missing_keys_across_records() -> None:
    entries = [
        _entry(1, metadata={"method": "GET"}),
        _entry(2, metadata={}),
        _entry(3, metadata={"path": "/health"}),
    ]
    distributions = compute_statistics(entries)["metadata_distributions"]
    # Absence is not a category: each key counts only the records that had it.
    assert distributions["method"] == [{"value": "GET", "count": 1}]
    assert distributions["path"] == [{"value": "/health", "count": 1}]


def test_metadata_none_values_are_excluded_not_categorized() -> None:
    entries = [
        _entry(1, metadata={"region": None}),
        _entry(2, metadata={"region": "eu"}),
        _entry(3, metadata={"region": "eu"}),
    ]
    assert compute_statistics(entries)["metadata_distributions"]["region"] == [
        {"value": "eu", "count": 2}
    ]


def test_metadata_key_with_only_none_values_is_omitted() -> None:
    entries = [_entry(1, metadata={"region": None}), _entry(2, metadata={"region": None})]
    assert compute_statistics(entries)["metadata_distributions"] == {}


def test_metadata_none_values_do_not_inflate_cardinality() -> None:
    # 21 real values + a batch of Nones stays low cardinality.
    entries = [_entry(i, metadata={"shard": f"s{i}"}) for i in range(21)]
    entries += [_entry(100 + i, metadata={"shard": None}) for i in range(5)]
    assert len(compute_statistics(entries)["metadata_distributions"]["shard"]) == 21


def test_metadata_scalar_value_types_are_supported() -> None:
    entries = [
        _entry(1, metadata={"status_code": 200, "cached": True, "load": 0.5, "az": "a"}),
        _entry(2, metadata={"status_code": 200, "cached": False, "load": 1.5, "az": "b"}),
    ]
    distributions = compute_statistics(entries)["metadata_distributions"]
    assert distributions["status_code"] == [{"value": 200, "count": 2}]
    assert _as_mapping(distributions["cached"]) == {True: 1, False: 1}
    assert _as_mapping(distributions["load"]) == {0.5: 1, 1.5: 1}
    assert _as_mapping(distributions["az"]) == {"a": 1, "b": 1}
    # Types survive; nothing is stringified.
    assert isinstance(distributions["status_code"][0]["value"], int)


def test_metadata_complex_values_are_skipped_without_crashing() -> None:
    entries = [
        _entry(1, metadata={"err": {"type": "Error", "message": "boom"}, "svc": "api"}),
        _entry(2, metadata={"tags": ["a", "b"], "svc": "api"}),
        _entry(3, metadata={"nested": {"deep": {"x": 1}}, "svc": "api"}),
    ]
    distributions = compute_statistics(entries)["metadata_distributions"]
    assert set(distributions) == {"svc"}
    assert distributions["svc"] == [{"value": "api", "count": 3}]


def test_metadata_key_mixing_scalar_and_complex_values_is_omitted() -> None:
    # A partial distribution would misreport the counts, so the whole key goes.
    entries = [
        _entry(1, metadata={"payload": "text"}),
        _entry(2, metadata={"payload": {"nested": True}}),
    ]
    assert compute_statistics(entries)["metadata_distributions"] == {}


def test_metadata_mixed_schemas_across_ecosystems() -> None:
    # Records from different emitters in one dataset: every key is discovered
    # on its own terms, none is assumed to exist.
    entries = [
        _entry(1, metadata={"method": "GET", "status_code": 200}),
        _entry(2, metadata={"thread": "http-nio-1", "pid": 12345}),
        _entry(3, metadata={"spid": 61, "severity": 13}),
    ]
    distributions = compute_statistics(entries)["metadata_distributions"]
    assert set(distributions) == {
        "method",
        "status_code",
        "thread",
        "pid",
        "spid",
        "severity",
    }


def test_metadata_keys_are_alphabetically_ordered() -> None:
    entries = [_entry(1, metadata={"zulu": 1, "alpha": 2, "mike": 3})]
    distributions = compute_statistics(entries)["metadata_distributions"]
    assert list(distributions) == ["alpha", "mike", "zulu"]


def test_metadata_empty_for_records_without_metadata() -> None:
    assert compute_statistics([_entry(1), _entry(2)])["metadata_distributions"] == {}


# ---------------------------------------------------------------------------
# Edge cases + determinism
# ---------------------------------------------------------------------------


def test_empty_parsed_logs_returns_valid_empty_structure() -> None:
    assert compute_statistics([]) == {
        "level_distribution": [],
        "logger_distribution": [],
        "severity": {
            "error_count": 0,
            "warning_count": 0,
            "error_ratio": 0.0,
            "warning_ratio": 0.0,
        },
        "timestamp_coverage": {
            "with_timestamp": 0,
            "without_timestamp": 0,
            "earliest": None,
            "latest": None,
        },
        "metadata_distributions": {},
    }


def test_all_fields_none_does_not_crash() -> None:
    entries = [_entry(1), _entry(2), _entry(3)]
    statistics = compute_statistics(entries)
    assert statistics["level_distribution"] == [{"value": None, "count": 3}]
    assert statistics["logger_distribution"] == [{"value": None, "count": 3}]
    assert statistics["timestamp_coverage"]["without_timestamp"] == 3


def test_deterministic_ordering_on_tied_counts() -> None:
    # Same counts everywhere: order must be by value, not by insertion or hash.
    entries = [
        _entry(1, level="DELTA"),
        _entry(2, level="ALPHA"),
        _entry(3, level="CHARLIE"),
        _entry(4, level="BRAVO"),
    ]
    assert _values(compute_statistics(entries)["level_distribution"]) == [
        "ALPHA",
        "BRAVO",
        "CHARLIE",
        "DELTA",
    ]


def test_deterministic_ordering_on_tied_metadata_counts() -> None:
    entries = [
        _entry(1, metadata={"az": "eu-west"}),
        _entry(2, metadata={"az": "ap-south"}),
        _entry(3, metadata={"az": "us-east"}),
    ]
    assert _values(compute_statistics(entries)["metadata_distributions"]["az"]) == [
        "ap-south",
        "eu-west",
        "us-east",
    ]


def test_ties_of_mixed_value_types_are_ordered_deterministically() -> None:
    entries = [
        _entry(1, metadata={"code": "200"}),
        _entry(2, metadata={"code": 404}),
        _entry(3, metadata={"code": 1.5}),
    ]
    first = compute_statistics(entries)["metadata_distributions"]["code"]
    second = compute_statistics(entries)["metadata_distributions"]["code"]
    assert first == second


def test_same_input_same_output() -> None:
    entries = [
        _entry(1, level="INFO", logger="a", timestamp=datetime(2026, 1, 1),
               metadata={"method": "GET"}),
        _entry(2, level="ERROR", logger="b", metadata={"method": "POST", "retry": True}),
        _entry(3, metadata={"payload": {"nested": 1}}),
    ]
    assert compute_statistics(entries) == compute_statistics(entries)
    assert statistics_node({"parsed_logs": entries}) == statistics_node(
        {"parsed_logs": entries}
    )


def test_record_order_does_not_change_the_result() -> None:
    entries = [
        _entry(1, level="INFO", metadata={"method": "GET"}),
        _entry(2, level="ERROR", metadata={"method": "POST"}),
        _entry(3, level="INFO", metadata={"method": "GET"}),
    ]
    assert compute_statistics(entries) == compute_statistics(list(reversed(entries)))


# ---------------------------------------------------------------------------
# End-to-end over the real sample corpus (parser -> statistics)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(p.name for p in _SAMPLE_DIR.glob("*.log")))
def test_runs_over_every_sample_log(name: str) -> None:
    # Real, ecosystem-diverse input must never crash the node, and the payload
    # must always stay JSON-serializable and well-formed.
    parsed = parser_node({"raw_logs": (_SAMPLE_DIR / name).read_text()})
    statistics = statistics_node(parsed)["statistics"]
    assert set(statistics) == EXPECTED_KEYS
    assert json.loads(json.dumps(statistics)) == statistics
    assert len(statistics["level_distribution"]) <= TOP_VALUE_LIMIT
    assert len(statistics["logger_distribution"]) <= TOP_VALUE_LIMIT
    for rows in statistics["metadata_distributions"].values():
        assert 1 <= len(rows) <= MAX_METADATA_CARDINALITY
    coverage = statistics["timestamp_coverage"]
    assert (
        coverage["with_timestamp"] + coverage["without_timestamp"]
        == len(parsed["parsed_logs"])
    )


def test_yearless_timestamps_count_as_without_timestamp() -> None:
    # The parser reports a yearless syslog stamp as ``None``; statistics simply
    # counts what it is given. No repair or inference happens on this side.
    parsed = parser_node(
        {
            "raw_logs": "\n".join(
                [
                    "Jan 10 14:52:31 INFO cache miss",
                    "2026-08-12T10:15:30Z INFO started",
                ]
            )
        }
    )
    assert [e["timestamp"] for e in parsed["parsed_logs"]] == [
        None,
        datetime(2026, 8, 12, 10, 15, 30, tzinfo=timezone.utc),
    ]
    coverage = statistics_node(parsed)["statistics"]["timestamp_coverage"]
    assert coverage == {
        "with_timestamp": 1,
        "without_timestamp": 1,
        "earliest": "2026-08-12T10:15:30+00:00",
        "latest": "2026-08-12T10:15:30+00:00",
    }


def test_timestamps_sample_span_excludes_yearless_records() -> None:
    # timestamps.log mixes ISO, space-separated, yearless syslog and garbage.
    # Only the two complete stamps may define the span — no year 1900 anywhere.
    parsed = parser_node({"raw_logs": (_SAMPLE_DIR / "timestamps.log").read_text()})
    coverage = statistics_node(parsed)["statistics"]["timestamp_coverage"]
    assert coverage["with_timestamp"] == 2
    assert coverage["without_timestamp"] == 2
    # Both complete stamps denote the same instant (one aware, one naive read as
    # UTC by the comparison layer), so the span collapses onto that instant.
    assert coverage["earliest"] == "2026-07-22T10:15:30+00:00"
    assert coverage["latest"] == "2026-07-22T10:15:30+00:00"


def test_no_sample_log_yields_a_year_1900_timestamp() -> None:
    # Corpus-wide regression guard against ``strptime``'s default epoch leaking
    # out of the parser as a real event time.
    for path in sorted(_SAMPLE_DIR.glob("*.log")):
        parsed = parser_node({"raw_logs": path.read_text()})
        for entry in parsed["parsed_logs"]:
            timestamp = entry["timestamp"]
            assert timestamp is None or timestamp.year != 1900, path.name


def test_fastapi_sample_metadata_is_discovered_not_hard_coded() -> None:
    parsed = parser_node({"raw_logs": (_SAMPLE_DIR / "fastapi.log").read_text()})
    statistics = statistics_node(parsed)["statistics"]
    # Uvicorn access metadata: low-cardinality keys are summarized...
    assert set(statistics["metadata_distributions"]) >= {"method", "status_code"}
    assert _as_mapping(statistics["metadata_distributions"]["status_code"]) == {
        200: 1,
        500: 1,
    }
    assert "INFO" in _values(statistics["level_distribution"])


def test_spring_boot_sample_high_cardinality_keys_are_omitted() -> None:
    parsed = parser_node(
        {"raw_logs": (_SAMPLE_DIR / "java_spring_boot_large.text.log").read_text()}
    )
    distributions = statistics_node(parsed)["statistics"]["metadata_distributions"]
    # Stable, low-cardinality dimensions survive; per-request identifiers do not.
    assert "scenario" in distributions
    for key, rows in distributions.items():
        assert len(rows) <= MAX_METADATA_CARDINALITY, key


def test_compiled_graph_runs_statistics_over_parsed_logs() -> None:
    raw = "\n".join(
        [
            json.dumps({"timestamp": "2026-01-01T00:00:00Z", "level": "info", "msg": "a"}),
            json.dumps({"timestamp": "2026-01-01T00:00:05Z", "level": "error", "msg": "b"}),
        ]
    )
    final_state = compile_graph().invoke({"application_name": "svc", "raw_logs": raw})
    statistics = final_state["statistics"]
    assert set(statistics) == EXPECTED_KEYS
    assert _as_mapping(statistics["level_distribution"]) == {"INFO": 1, "ERROR": 1}
    assert statistics["severity"]["error_count"] == 1
    assert statistics["timestamp_coverage"]["with_timestamp"] == 2
    assert "statistics" in final_state["completed_stages"]

"""Unit tests for the deterministic Timeline Node (``timeline/``).

Entries are built with :func:`_entry`, which mirrors the ``ParsedLogEntry``
schema the parser emits, so these tests exercise the node against exactly the
shape it receives in the graph.

Documented conventions asserted here:

    * a ``None`` timestamp is *never* repaired, inferred or re-parsed — the
      entry is excluded from the timeline and reported in the notes;
    * the bucket width is derived from the log span alone, on the exact
      boundaries the specification fixes;
    * the bucket series is contiguous internally (so recovery can be detected)
      but only *populated* windows are emitted as events, and the omission is
      stated in the notes rather than left silent;
    * every event carries the counts of the window it describes, so a consumer
      reads milestones and buckets the same way.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from graph import compile_graph
from models import ParsedLogEntry
from parser.parser_node import parser_node
from timeline import (
    COARSE_BUCKET,
    FINE_BUCKET,
    MEDIUM_BUCKET,
    NO_TIMESTAMPS_NOTE,
    SAMPLE_MESSAGE_LIMIT,
    SAMPLE_MESSAGE_MAX_LENGTH,
    TOP_LOGGER_LIMIT,
    WIDE_BUCKET,
    build_buckets,
    build_timeline,
    resolve_narrative,
    select_bucket_size,
    timestamped_entries,
)
from timeline.node import timeline_node

_SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_logs"

BASE = datetime(2024, 1, 1, 12, 0, 0)

EXPECTED_EVENT_KEYS = {
    "event_type",
    "timestamp",
    "end_timestamp",
    "milestone_kind",
    "total_logs",
    "error_count",
    "warning_count",
    "top_loggers",
    "sample_messages",
    "summary",
}


def _entry(
    line_number: int = 1,
    *,
    timestamp: datetime | None = None,
    level: str | None = None,
    logger: str | None = None,
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


def _minute_series(
    error_counts: list[int], *, base: datetime = BASE
) -> list[ParsedLogEntry]:
    """One INFO entry per minute, plus ``error_counts[i]`` errors in minute *i*.

    The resulting span is ``len(error_counts) - 1`` minutes, which selects the
    1-minute bucket, so minute *i* and bucket *i* line up one-to-one.
    """
    entries: list[ParsedLogEntry] = []
    line = 0
    for minute, errors in enumerate(error_counts):
        line += 1
        entries.append(
            _entry(line, timestamp=base + timedelta(minutes=minute), level="INFO")
        )
        for offset in range(errors):
            line += 1
            entries.append(
                _entry(
                    line,
                    timestamp=base + timedelta(minutes=minute, seconds=offset + 1),
                    level="ERROR",
                    logger="svc.db",
                    message=f"failure {minute}.{offset}",
                )
            )
    return entries


def _milestones(timeline: list[dict]) -> dict[str, dict]:
    """Index the milestone events of a timeline by their kind."""
    return {
        event["milestone_kind"]: event
        for event in timeline
        if event["event_type"] == "milestone"
    }


def _buckets(timeline: list[dict]) -> list[dict]:
    return [event for event in timeline if event["event_type"] == "bucket"]


# ---------------------------------------------------------------------------
# Node contract
# ---------------------------------------------------------------------------


def test_return_shape_and_completed_stage() -> None:
    result = timeline_node({"parsed_logs": [_entry(timestamp=BASE)]})
    assert set(result) == {"timeline", "investigation_notes", "completed_stages"}
    assert result["completed_stages"] == ["timeline"]


def test_every_event_carries_the_full_stable_shape() -> None:
    result = timeline_node({"parsed_logs": _minute_series([0, 0, 2, 5, 0, 0])})
    assert result["timeline"]
    for event in result["timeline"]:
        assert set(event) == EXPECTED_EVENT_KEYS


def test_timeline_is_json_serializable() -> None:
    result = timeline_node({"parsed_logs": _minute_series([0, 3, 0, 0, 0, 0])})
    # Timestamps must have left the node as ISO strings, not datetimes.
    assert json.loads(json.dumps(result["timeline"])) == result["timeline"]


def test_node_does_not_mutate_input_state() -> None:
    parsed_logs = _minute_series([0, 2, 0, 0, 0, 0])
    snapshot = [dict(entry) for entry in parsed_logs]
    state = {"parsed_logs": parsed_logs, "parser_metrics": {"missing_timestamp_lines": 0}}

    timeline_node(state)

    assert parsed_logs == snapshot
    assert set(state) == {"parsed_logs", "parser_metrics"}


def test_is_deterministic_across_runs() -> None:
    parsed_logs = _minute_series([0, 1, 4, 9, 2, 0, 0])
    assert timeline_node({"parsed_logs": parsed_logs}) == timeline_node(
        {"parsed_logs": list(parsed_logs)}
    )


# ---------------------------------------------------------------------------
# Zero-state: nothing can be placed on a time axis
# ---------------------------------------------------------------------------


def test_zero_timestamped_logs_skips_the_timeline() -> None:
    parsed_logs = [
        _entry(1, level="INFO"),
        _entry(2, level="ERROR"),
    ]
    result = timeline_node(
        {"parsed_logs": parsed_logs, "parser_metrics": {"missing_timestamp_lines": 2}}
    )

    assert result == {
        "timeline": [],
        "investigation_notes": [NO_TIMESTAMPS_NOTE],
        "completed_stages": ["timeline"],
    }


def test_empty_parsed_logs_skips_the_timeline() -> None:
    assert timeline_node({"parsed_logs": []})["investigation_notes"] == [
        NO_TIMESTAMPS_NOTE
    ]


def test_missing_parsed_logs_key_is_tolerated() -> None:
    assert timeline_node({})["timeline"] == []


# ---------------------------------------------------------------------------
# Single entry
# ---------------------------------------------------------------------------


def test_single_timestamped_log_yields_one_bucket_and_both_boundaries() -> None:
    result = timeline_node(
        {"parsed_logs": [_entry(1, timestamp=BASE, level="INFO", logger="svc.a")]}
    )
    timeline = result["timeline"]
    milestones = _milestones(timeline)

    assert set(milestones) == {"logs_start", "logs_end"}
    assert milestones["logs_start"]["timestamp"] == BASE.isoformat()
    assert milestones["logs_end"]["timestamp"] == BASE.isoformat()

    buckets = _buckets(timeline)
    assert len(buckets) == 1
    assert buckets[0]["total_logs"] == 1
    assert buckets[0]["summary"] == "1 log (0 errors, 0 warnings)"


def test_single_error_entry_reports_the_full_error_narrative() -> None:
    result = timeline_node(
        {"parsed_logs": [_entry(1, timestamp=BASE, level="ERROR", logger="svc.db")]}
    )
    milestones = _milestones(result["timeline"])

    # Onset and peak exist (one error is both); recovery never happens because
    # no later window is observed at all.
    assert set(milestones) == {
        "logs_start",
        "logs_end",
        "first_error",
        "last_error",
        "error_onset",
        "peak_error_volume",
    }


# ---------------------------------------------------------------------------
# Adaptive granularity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        (timedelta(0), FINE_BUCKET),
        (timedelta(seconds=30), FINE_BUCKET),
        (timedelta(minutes=5, microseconds=-1), FINE_BUCKET),
        (timedelta(minutes=5), MEDIUM_BUCKET),
        (timedelta(minutes=42), MEDIUM_BUCKET),
        (timedelta(hours=1), MEDIUM_BUCKET),
        (timedelta(hours=1, seconds=1), COARSE_BUCKET),
        (timedelta(hours=24), COARSE_BUCKET),
        (timedelta(hours=24, seconds=1), WIDE_BUCKET),
        (timedelta(days=30), WIDE_BUCKET),
    ],
)
def test_bucket_size_boundaries(span: timedelta, expected: timedelta) -> None:
    assert select_bucket_size(span) == expected


def test_short_span_uses_ten_second_buckets_end_to_end() -> None:
    parsed_logs = [
        _entry(index + 1, timestamp=BASE + timedelta(seconds=index * 7), level="INFO")
        for index in range(10)
    ]
    result = timeline_node({"parsed_logs": parsed_logs})
    buckets = _buckets(result["timeline"])

    # 63 seconds of logs at a 10-second granularity.
    assert len(buckets) == 7
    starts = [event["timestamp"] for event in buckets]
    assert starts[0] == BASE.isoformat()
    assert starts[-1] == (BASE + timedelta(seconds=60)).isoformat()


def test_multi_day_span_uses_hourly_buckets() -> None:
    parsed_logs = [
        _entry(1, timestamp=BASE, level="INFO"),
        _entry(2, timestamp=BASE + timedelta(days=3), level="INFO"),
    ]
    result = timeline_node({"parsed_logs": parsed_logs})
    buckets = _buckets(result["timeline"])

    assert len(buckets) == 2  # only the two populated hours are emitted
    assert buckets[0]["end_timestamp"] == (BASE + timedelta(hours=1)).isoformat()
    assert "1 hour" in result["investigation_notes"][0]


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------


def test_buckets_are_aligned_to_clock_boundaries() -> None:
    offset_base = BASE + timedelta(seconds=7)
    entries = timestamped_entries([_entry(1, timestamp=offset_base, level="INFO")])
    buckets = build_buckets(entries, FINE_BUCKET)

    # The first bucket starts at 12:00:00, not at the 12:00:07 first log line.
    assert buckets[0].start == BASE
    assert buckets[0].end == BASE + FINE_BUCKET


def test_bucket_series_is_contiguous_including_empty_windows() -> None:
    entries = timestamped_entries(
        [
            _entry(1, timestamp=BASE, level="INFO"),
            _entry(2, timestamp=BASE + timedelta(minutes=5), level="INFO"),
        ]
    )
    buckets = build_buckets(entries, MEDIUM_BUCKET)

    assert [bucket.index for bucket in buckets] == list(range(6))
    assert [bucket.total_logs for bucket in buckets] == [1, 0, 0, 0, 0, 1]
    for earlier, later in zip(buckets, buckets[1:]):
        assert earlier.end == later.start


def test_empty_windows_are_omitted_from_events_and_reported() -> None:
    parsed_logs = [
        _entry(1, timestamp=BASE, level="INFO"),
        _entry(2, timestamp=BASE + timedelta(minutes=5), level="INFO"),
    ]
    result = timeline_node({"parsed_logs": parsed_logs})

    assert len(_buckets(result["timeline"])) == 2
    assert any(
        "4 of 6 windows contained no logs" in note
        for note in result["investigation_notes"]
    )


def test_bucket_counts_split_errors_warnings_and_totals() -> None:
    parsed_logs = [
        _entry(1, timestamp=BASE, level="INFO"),
        _entry(2, timestamp=BASE + timedelta(seconds=1), level="ERROR"),
        _entry(3, timestamp=BASE + timedelta(seconds=2), level="CRITICAL"),
        _entry(4, timestamp=BASE + timedelta(seconds=3), level="FATAL"),
        _entry(5, timestamp=BASE + timedelta(seconds=4), level="WARN"),
        _entry(6, timestamp=BASE + timedelta(seconds=5), level="WARNING"),
        _entry(7, timestamp=BASE + timedelta(seconds=6), level=None),
    ]
    bucket = _buckets(timeline_node({"parsed_logs": parsed_logs})["timeline"])[0]

    assert bucket["total_logs"] == 7
    # CRITICAL and FATAL are error-class for timeline purposes.
    assert bucket["error_count"] == 3
    assert bucket["warning_count"] == 2
    assert bucket["summary"] == "7 logs (3 errors, 2 warnings)"


def test_top_loggers_are_capped_and_ranked_by_error_volume() -> None:
    parsed_logs = [
        # 'chatty' has the most lines, but 'db' produces the errors.
        *[
            _entry(i, timestamp=BASE + timedelta(seconds=i), level="INFO", logger="chatty")
            for i in range(1, 11)
        ],
        *[
            _entry(20 + i, timestamp=BASE + timedelta(seconds=i), level="ERROR", logger="db")
            for i in range(1, 4)
        ],
        _entry(40, timestamp=BASE, level="INFO", logger="api"),
        _entry(41, timestamp=BASE, level="INFO", logger="web"),
        _entry(42, timestamp=BASE, level="INFO", logger=None),
    ]
    result = timeline_node({"parsed_logs": parsed_logs})
    top = _milestones(result["timeline"])["peak_error_volume"]["top_loggers"]

    assert len(top) <= TOP_LOGGER_LIMIT
    assert top[0] == "db"
    assert "chatty" in top
    assert None not in top


def test_sample_messages_prefer_errors_and_are_capped() -> None:
    parsed_logs = [
        _entry(1, timestamp=BASE, level="INFO", message="startup complete"),
        _entry(2, timestamp=BASE + timedelta(seconds=1), level="ERROR", message="first boom"),
        _entry(3, timestamp=BASE + timedelta(seconds=2), level="ERROR", message="second boom"),
        _entry(4, timestamp=BASE + timedelta(seconds=3), level="ERROR", message="third boom"),
    ]
    bucket = _buckets(timeline_node({"parsed_logs": parsed_logs})["timeline"])[0]

    assert len(bucket["sample_messages"]) == SAMPLE_MESSAGE_LIMIT
    assert bucket["sample_messages"] == ["first boom", "second boom"]


def test_sample_messages_are_truncated() -> None:
    parsed_logs = [_entry(1, timestamp=BASE, level="ERROR", message="x" * 5000)]
    bucket = _buckets(timeline_node({"parsed_logs": parsed_logs})["timeline"])[0]
    (preview,) = bucket["sample_messages"]

    assert len(preview) == SAMPLE_MESSAGE_MAX_LENGTH
    assert preview.endswith("...")


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------


def test_boundary_milestones_use_the_first_and_last_usable_timestamps() -> None:
    parsed_logs = [
        _entry(1, timestamp=BASE + timedelta(minutes=9), level="INFO"),  # out of order
        _entry(2, timestamp=BASE, level="INFO"),
        _entry(3, level="INFO"),  # unbucketable, must not become a boundary
    ]
    milestones = _milestones(timeline_node({"parsed_logs": parsed_logs})["timeline"])

    assert milestones["logs_start"]["timestamp"] == BASE.isoformat()
    assert milestones["logs_end"]["timestamp"] == (
        BASE + timedelta(minutes=9)
    ).isoformat()


def test_first_and_last_error_point_at_error_entries() -> None:
    parsed_logs = _minute_series([0, 0, 2, 4, 1, 0, 0])
    milestones = _milestones(timeline_node({"parsed_logs": parsed_logs})["timeline"])

    assert milestones["first_error"]["timestamp"] == (
        BASE + timedelta(minutes=2, seconds=1)
    ).isoformat()
    assert milestones["last_error"]["timestamp"] == (
        BASE + timedelta(minutes=4, seconds=1)
    ).isoformat()
    assert milestones["first_error"]["error_count"] == 1
    assert "failure 2.0" in milestones["first_error"]["sample_messages"]


def test_onset_peak_and_recovery_land_on_the_right_windows() -> None:
    # errors per minute:  0  0  0  4  8  1  0  0  0  0
    parsed_logs = _minute_series([0, 0, 0, 4, 8, 1, 0, 0, 0, 0])
    milestones = _milestones(timeline_node({"parsed_logs": parsed_logs})["timeline"])

    assert milestones["error_onset"]["timestamp"] == (
        BASE + timedelta(minutes=3)
    ).isoformat()
    assert milestones["error_onset"]["error_count"] == 4

    assert milestones["peak_error_volume"]["timestamp"] == (
        BASE + timedelta(minutes=4)
    ).isoformat()
    assert milestones["peak_error_volume"]["error_count"] == 8

    # Baseline before the onset is zero, so recovery is the first error-free
    # window after the peak — minute 5 still has one error.
    assert milestones["recovery_onset"]["timestamp"] == (
        BASE + timedelta(minutes=6)
    ).isoformat()
    assert milestones["recovery_onset"]["error_count"] == 0


def test_narrative_indices_are_resolved_from_the_bucket_series() -> None:
    entries = timestamped_entries(_minute_series([0, 0, 0, 4, 8, 1, 0, 0, 0, 0]))
    narrative = resolve_narrative(build_buckets(entries, MEDIUM_BUCKET))

    assert narrative is not None
    assert (narrative.onset_index, narrative.peak_index) == (3, 4)
    assert narrative.baseline == 0.0
    assert narrative.recovery_index == 6


def test_peak_ties_resolve_to_the_earliest_window() -> None:
    entries = timestamped_entries(_minute_series([0, 5, 0, 5, 0, 0]))
    narrative = resolve_narrative(build_buckets(entries, MEDIUM_BUCKET))

    assert narrative is not None
    assert narrative.peak_index == 1


def test_recovery_is_omitted_when_errors_never_subside() -> None:
    # Errors continue right through the final window.
    parsed_logs = _minute_series([0, 3, 9, 2, 2, 2])
    milestones = _milestones(timeline_node({"parsed_logs": parsed_logs})["timeline"])

    assert "peak_error_volume" in milestones
    assert "recovery_onset" not in milestones


def test_error_milestones_are_omitted_when_there_are_no_errors() -> None:
    result = timeline_node({"parsed_logs": _minute_series([0, 0, 0, 0, 0, 0])})
    milestones = _milestones(result["timeline"])

    assert set(milestones) == {"logs_start", "logs_end"}
    assert any(
        "no error-level entries" in note for note in result["investigation_notes"]
    )


def test_onset_falls_on_the_first_error_window_even_with_a_later_spike() -> None:
    # A low background rate from the very first window: the breakout the node
    # reports is where errors first appear, and the baseline it compares
    # against is zero because nothing preceded them.
    entries = timestamped_entries(_minute_series([1, 1, 1, 1, 12, 0]))
    narrative = resolve_narrative(build_buckets(entries, MEDIUM_BUCKET))

    assert narrative is not None
    assert narrative.onset_index == 0
    assert narrative.baseline == 0.0
    assert narrative.peak_index == 4


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_events_are_ordered_chronologically() -> None:
    result = timeline_node({"parsed_logs": _minute_series([0, 2, 7, 1, 0, 0])})
    stamps = [event["timestamp"] for event in result["timeline"]]

    assert stamps == sorted(stamps)


def test_milestones_precede_the_bucket_they_share_an_instant_with() -> None:
    result = timeline_node({"parsed_logs": [_entry(1, timestamp=BASE, level="ERROR")]})
    same_instant = [
        event for event in result["timeline"] if event["timestamp"] == BASE.isoformat()
    ]

    assert same_instant[-1]["event_type"] == "bucket"
    assert [event["milestone_kind"] for event in same_instant[:-1]] == [
        "logs_start",
        "first_error",
        "error_onset",
        "peak_error_volume",
        "last_error",
        "logs_end",
    ]


# ---------------------------------------------------------------------------
# Timestamps are consumed, never repaired
# ---------------------------------------------------------------------------


def test_entries_without_timestamps_are_excluded_not_repaired() -> None:
    parsed_logs = [
        _entry(1, timestamp=BASE, level="INFO"),
        _entry(2, level="ERROR", message="unstamped failure"),
        _entry(3, timestamp=BASE + timedelta(seconds=5), level="INFO"),
    ]
    result = timeline_node({"parsed_logs": parsed_logs})

    assert sum(event["total_logs"] for event in _buckets(result["timeline"])) == 2
    assert "unstamped failure" not in json.dumps(result["timeline"])
    # No error milestones: the only error in the payload was unbucketable.
    assert set(_milestones(result["timeline"])) == {"logs_start", "logs_end"}


def test_mixed_naive_and_aware_timestamps_share_one_axis() -> None:
    parsed_logs = [
        _entry(1, timestamp=BASE.replace(tzinfo=timezone.utc), level="INFO"),
        _entry(2, timestamp=BASE + timedelta(seconds=2), level="INFO"),
    ]
    result = timeline_node({"parsed_logs": parsed_logs})

    # Naive values are read as UTC for ordering only; both land in one window.
    assert len(_buckets(result["timeline"])) == 1
    assert _buckets(result["timeline"])[0]["total_logs"] == 2


# ---------------------------------------------------------------------------
# Investigation notes / data quality
# ---------------------------------------------------------------------------


def test_notes_report_the_selected_granularity() -> None:
    result = timeline_node({"parsed_logs": _minute_series([0, 0, 0, 0, 0, 0])})

    assert result["investigation_notes"][0] == (
        "Timeline: bucketed 6 timestamped entries into 6 windows of 1 minute, "
        f"spanning {BASE.isoformat()} to "
        f"{(BASE + timedelta(minutes=5)).isoformat()}."
    )


def test_data_quality_warning_uses_parser_metrics() -> None:
    state = {
        "parsed_logs": [_entry(1, timestamp=BASE, level="INFO")],
        "parser_metrics": {"missing_timestamp_lines": 7},
    }
    notes = timeline_node(state)["investigation_notes"]

    assert notes[1] == (
        "Data Quality Warning: 7 entries omitted complete timestamps and were "
        "excluded from timeline analysis."
    )


def test_data_quality_warning_is_singular_for_one_entry() -> None:
    state = {
        "parsed_logs": [_entry(1, timestamp=BASE, level="INFO"), _entry(2)],
        "parser_metrics": {"missing_timestamp_lines": 1},
    }
    notes = timeline_node(state)["investigation_notes"]

    assert notes[1] == (
        "Data Quality Warning: 1 entry omitted a complete timestamp and was "
        "excluded from timeline analysis."
    )


def test_data_quality_warning_falls_back_to_counting_entries() -> None:
    # No ``parser_metrics`` (isolated invocation): the count is recomputed.
    state = {"parsed_logs": [_entry(1, timestamp=BASE, level="INFO"), _entry(2)]}
    notes = timeline_node(state)["investigation_notes"]

    assert any("1 entry omitted a complete timestamp" in note for note in notes)


def test_no_data_quality_warning_when_every_entry_is_stamped() -> None:
    state = {
        "parsed_logs": _minute_series([0, 1, 0, 0, 0, 0]),
        "parser_metrics": {"missing_timestamp_lines": 0},
    }
    notes = timeline_node(state)["investigation_notes"]

    assert not any(note.startswith("Data Quality Warning") for note in notes)


def test_yearless_timestamps_from_the_parser_become_a_data_quality_warning() -> None:
    # ``timestamps.log`` mixes an ISO stamp, a naive stamp, a yearless syslog
    # stamp the parser deliberately refuses, and an unparseable one.
    raw_logs = (_SAMPLE_DIR / "timestamps.log").read_text()
    parsed = parser_node({"raw_logs": raw_logs})
    assert parsed["parser_metrics"]["missing_timestamp_lines"] == 2

    result = timeline_node(parsed)

    assert any(
        note
        == (
            "Data Quality Warning: 2 entries omitted complete timestamps and "
            "were excluded from timeline analysis."
        )
        for note in result["investigation_notes"]
    )
    assert sum(event["total_logs"] for event in _buckets(result["timeline"])) == 2


# ---------------------------------------------------------------------------
# Helper-level contract
# ---------------------------------------------------------------------------


def test_build_timeline_returns_events_and_notes() -> None:
    timeline, notes = build_timeline(_minute_series([0, 1, 0, 0, 0, 0]))

    assert timeline and notes
    assert notes[0].startswith("Timeline: bucketed")


def test_timestamped_entries_sorts_and_filters() -> None:
    parsed_logs = [
        _entry(3, timestamp=BASE + timedelta(seconds=10)),
        _entry(1, timestamp=BASE),
        _entry(2),  # no timestamp
    ]
    assert [entry["line_number"] for entry in timestamped_entries(parsed_logs)] == [1, 3]


# ---------------------------------------------------------------------------
# Graph integration
# ---------------------------------------------------------------------------


def test_timeline_runs_inside_the_compiled_graph() -> None:
    raw_logs = (_SAMPLE_DIR / "simple.log").read_text()
    result = compile_graph().invoke({"application_name": "demo", "raw_logs": raw_logs})

    assert "timeline" in result["completed_stages"]
    assert result["timeline"]
    milestones = _milestones(result["timeline"])
    assert {"logs_start", "logs_end", "first_error", "last_error"} <= set(milestones)

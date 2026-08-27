"""Tests for the Pattern Analysis Node (``graph_library/pattern_analysis/``).

Everything here runs offline. One seam is stubbed and nothing else:
``graph_library.pattern_analysis.node.iter_error_analysis_llms`` — a fake chat
model that records the prompt it was given and answers with whatever the test
told it to. Below that, the prompt builder and the deterministic fallback are
driven directly, because both are pure functions over the two upstream payloads
and neither needs a model to be exercised.

The conventions asserted here:

    * the prompt carries *both* deterministic reports and the notes describing
      their limits, because a pattern claim is only as good as the coverage
      behind it;
    * a long incident is trimmed to fit, milestones are never the thing trimmed,
      and the trimming is stated in the prompt rather than hidden;
    * the node degrades rather than fails — an absent provider, a raising call
      or an empty input all still publish a well-formed ``pattern_summary``,
      with the reason in ``investigation_notes``;
    * the degraded shape and the healthy shape are identical, so a downstream
      consumer never has to ask which one it got.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from graph import compile_graph
from graph_library.models import (
    PatternAnalysisResult,
    PatternSummary,
    Statistics,
    SystemAnomaly,
    TimelineEvent,
)
from graph_library.pattern_analysis import (
    DOMINANCE_RATIO,
    MAX_INVESTIGATION_NOTES,
    MAX_TIMELINE_BUCKETS,
    NO_INPUT_NOTE,
    SPIKE_RATIO,
    build_fallback_summary,
    build_pattern_analysis_prompt,
    format_investigation_notes,
    format_statistics,
    format_timeline,
    pattern_analysis_node,
    prompt_payload_sizes,
    severity_for_error_share,
)

# ===========================================================================
# Payload builders
# ===========================================================================


def _statistics(
    *,
    error_count: int = 4,
    warning_count: int = 1,
    error_ratio: float = 0.4,
    loggers: list[tuple[Any, int]] | None = None,
    metadata: dict[str, list[tuple[Any, int]]] | None = None,
) -> Statistics:
    """A ``Statistics`` payload with the fields these tests care about."""
    return {
        "level_distribution": [
            {"value": "ERROR", "count": error_count},
            {"value": "INFO", "count": 5},
        ],
        "logger_distribution": [
            {"value": value, "count": count}
            for value, count in (loggers or [("payment", 3), ("orders", 2)])
        ],
        "severity": {
            "error_count": error_count,
            "warning_count": warning_count,
            "error_ratio": error_ratio,
            "warning_ratio": 0.1,
        },
        "timestamp_coverage": {
            "with_timestamp": 10,
            "without_timestamp": 0,
            "earliest": "2026-01-01T10:00:00+00:00",
            "latest": "2026-01-01T10:30:00+00:00",
        },
        "metadata_distributions": {
            key: [{"value": value, "count": count} for value, count in rows]
            for key, rows in (metadata or {}).items()
        },
    }


def _bucket(
    timestamp: str,
    *,
    errors: int = 0,
    total: int | None = None,
    loggers: list[str] | None = None,
) -> TimelineEvent:
    return {
        "event_type": "bucket",
        "timestamp": timestamp,
        "end_timestamp": timestamp,
        "milestone_kind": None,
        "total_logs": total if total is not None else max(errors, 1),
        "error_count": errors,
        "warning_count": 0,
        "top_loggers": loggers or [],
        "sample_messages": ["something happened"],
        "summary": f"{errors} error(s) in the window starting {timestamp}.",
    }


def _milestone(kind: str, timestamp: str, *, loggers: list[str] | None = None) -> TimelineEvent:
    return {
        "event_type": "milestone",
        "timestamp": timestamp,
        "end_timestamp": None,
        "milestone_kind": kind,
        "total_logs": 1,
        "error_count": 1,
        "warning_count": 0,
        "top_loggers": loggers or [],
        "sample_messages": [],
        "summary": f"{kind} at {timestamp}.",
    }


#: A small, ordinary incident: quiet, then a burst across two components, then
#: recovery. Used wherever a test needs "a realistic timeline" and does not care
#: about the specific numbers.
INCIDENT_TIMELINE: list[TimelineEvent] = [
    _milestone("logs_start", "2026-01-01T10:00:00+00:00"),
    _milestone("first_error", "2026-01-01T10:10:00+00:00", loggers=["payment"]),
    _milestone("error_onset", "2026-01-01T10:10:00+00:00", loggers=["payment"]),
    _milestone("peak_error_volume", "2026-01-01T10:20:00+00:00", loggers=["orders"]),
    _milestone("recovery_onset", "2026-01-01T10:30:00+00:00"),
    _milestone("logs_end", "2026-01-01T10:40:00+00:00"),
    _bucket("2026-01-01T10:00:00+00:00", errors=0, total=8),
    _bucket("2026-01-01T10:10:00+00:00", errors=2, total=9, loggers=["payment"]),
    _bucket("2026-01-01T10:20:00+00:00", errors=9, total=20, loggers=["orders", "payment"]),
    _bucket("2026-01-01T10:30:00+00:00", errors=0, total=7),
]


# ===========================================================================
# Test doubles
# ===========================================================================


class _Recorder:
    """Every structured call the node made, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    @property
    def human_prompt(self) -> str:
        assert len(self.calls) == 1, f"expected 1 call, got {len(self.calls)}"
        return self.calls[0][2]

    @property
    def system_prompt(self) -> str:
        assert len(self.calls) == 1, f"expected 1 call, got {len(self.calls)}"
        return self.calls[0][1]


class _FakeStructuredLLM:
    def __init__(self, schema: Any, answer: Any, recorder: _Recorder) -> None:
        self._schema = schema
        self._answer = answer
        self._recorder = recorder

    def invoke(self, messages: Any) -> Any:
        system, human = (str(content) for _role, content in messages)
        self._recorder.calls.append((self._schema.__name__, system, human))
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


class _FakeLLM:
    def __init__(self, answer: Any, recorder: _Recorder) -> None:
        self._answer = answer
        self._recorder = recorder

    def with_structured_output(self, schema: Any, **_: Any) -> _FakeStructuredLLM:
        return _FakeStructuredLLM(schema, self._answer, self._recorder)


def _install_llm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    answer: Any,
    models: list[str] | None = None,
) -> _Recorder:
    """Point the pattern-analysis node at a fake model.

    Args:
        answer: What the call should return — a ``PatternAnalysisResult``, a
            plain dict, ``None``, or an exception for it to raise. A list of
            answers is consumed one per candidate model.
        models: The candidate ids the factory should yield, best first.
    """
    recorder = _Recorder()
    answers = list(answer) if isinstance(answer, list) else None

    def factory(*_args: Any, **_kwargs: Any) -> Any:
        for index, model in enumerate(models or ["primary-model"]):
            reply = answers[index] if answers else answer
            yield model, _FakeLLM(reply, recorder)

    monkeypatch.setattr(
        "graph_library.pattern_analysis.node.iter_error_analysis_llms", factory
    )
    return recorder


RESULT = PatternAnalysisResult(
    anomalies=[
        SystemAnomaly(
            category="logger_cascade",
            severity="critical",
            description="payment failed first, then orders.",
            affected_loggers=["payment", "orders"],
            time_window="2026-01-01T10:10:00+00:00",
        )
    ],
    cross_logger_correlations=["payment precedes orders by one bucket."],
    metadata_insights=["endpoint=/checkout carries most failures."],
    behavioral_synthesis="A payment outage propagated into the order service.",
)


# ===========================================================================
# Prompt and context formatting
# ===========================================================================


def test_statistics_are_rendered_field_by_field() -> None:
    rendered = format_statistics(
        _statistics(metadata={"endpoint": [("/checkout", 9), ("/health", 1)]})
    )

    assert rendered.startswith("STATISTICS")
    # Every field of the payload is evidence for at least one pattern kind, so
    # every field has to survive the trip into the prompt.
    for marker in (
        "level_distribution",
        "logger_distribution",
        "severity",
        "timestamp_coverage",
        "metadata_distributions",
    ):
        assert marker in rendered

    # The values, not just the keys.
    assert "payment" in rendered
    assert "/checkout" in rendered
    assert "2026-01-01T10:00:00+00:00" in rendered

    # Parseable as JSON below the heading, which is what lets the model align a
    # logger name in the statistics with the same name in the timeline.
    payload = json.loads(rendered.split("\n", 1)[1])
    assert payload["severity"]["error_count"] == 4


def test_absent_statistics_are_stated_rather_than_rendered_empty() -> None:
    # "{}" would read as "the dataset was empty"; the section has to say that
    # the report itself is missing.
    for empty in (None, {}):
        rendered = format_statistics(empty)  # type: ignore[arg-type]
        assert "unavailable" in rendered
        assert "{}" not in rendered


def test_timeline_renders_milestones_and_buckets_separately() -> None:
    rendered = format_timeline(INCIDENT_TIMELINE)

    milestone_heading = rendered.index("TIMELINE — 6 milestone(s)")
    bucket_heading = rendered.index("TIMELINE — 4 time bucket(s)")
    # Milestones first: they are the answer, the buckets are the evidence.
    assert milestone_heading < bucket_heading

    for marker in ("error_onset", "peak_error_volume", "recovery_onset"):
        assert marker in rendered
    for marker in ("event_type", "top_loggers", "sample_messages", "summary"):
        assert marker in rendered
    assert "orders" in rendered


def test_timeline_omits_the_constant_window_width() -> None:
    # ``end_timestamp`` is the same offset on every bucket of a run; repeating
    # it costs tokens and tells the model nothing the sequence does not.
    assert "end_timestamp" not in format_timeline(INCIDENT_TIMELINE)


def test_absent_timeline_is_stated_rather_than_rendered_empty() -> None:
    for empty in (None, []):
        rendered = format_timeline(empty)
        assert "unavailable" in rendered


def test_a_long_series_is_trimmed_to_the_busiest_buckets() -> None:
    # 200 quiet buckets, comfortably past the cap, with one loud one buried
    # late in the series. The loud one is the incident and must survive; the
    # omission must be stated.
    def at(index: int) -> str:
        return f"2026-01-01T{index // 60:02d}:{index % 60:02d}:00+00:00"

    buckets = [_bucket(at(index), errors=0) for index in range(200)]
    buckets[150] = _bucket(at(150), errors=99, loggers=["payment"])
    timeline = [_milestone("logs_start", at(0)), *buckets]

    rendered = format_timeline(timeline)

    assert "omitted to fit" in rendered
    assert "not contiguous" in rendered
    # The loud bucket is kept, and the milestone is untouched.
    assert "99" in rendered
    assert "logs_start" in rendered

    # The bucket array is the last top-level JSON value; nested arrays are
    # indented, so anchoring on a line-initial bracket picks the right one.
    payload = json.loads(rendered[rendered.rindex("\n[\n") + 1 :])
    assert len(payload) == MAX_TIMELINE_BUCKETS
    # Chronological order is restored after the busiest-first selection: the
    # sequence is the evidence, and a volume-sorted series would invite the
    # coincidence-as-causation error the prompt warns against.
    assert payload == sorted(payload, key=lambda event: event["timestamp"])


def test_a_short_series_is_sent_whole_and_says_nothing_about_omission() -> None:
    rendered = format_timeline(INCIDENT_TIMELINE)
    assert "omitted to fit" not in rendered


def test_investigation_notes_are_rendered_and_capped() -> None:
    assert "(none)" in format_investigation_notes(None)
    assert "(none)" in format_investigation_notes([])

    rendered = format_investigation_notes(["parser skipped 3 lines", "no timestamps"])
    assert "- parser skipped 3 lines" in rendered
    assert "- no timestamps" in rendered

    many = [f"note {index}" for index in range(MAX_INVESTIGATION_NOTES + 5)]
    capped = format_investigation_notes(many)
    assert "5 further note(s), omitted" in capped


def test_the_prompt_carries_both_reports_and_the_notes() -> None:
    prompt = build_pattern_analysis_prompt(
        _statistics(),
        INCIDENT_TIMELINE,
        ["Data Quality Warning: 4 entries had no timestamp"],
        application_name="checkout-api",
    )

    assert "Application under investigation: checkout-api" in prompt
    assert "STATISTICS" in prompt
    assert "TIMELINE" in prompt
    assert "INVESTIGATION NOTES" in prompt
    # The notes are in the prompt because a distribution says nothing about the
    # records that never reached it.
    assert "4 entries had no timestamp" in prompt
    # And the question is actually asked.
    assert "cross-logger cascades" in prompt


def test_the_prompt_is_well_formed_for_empty_inputs() -> None:
    # The node decides whether an empty payload is worth a call; the builder
    # does not second-guess it by raising.
    prompt = build_pattern_analysis_prompt(None, None, None)
    assert "STATISTICS" in prompt
    assert "TIMELINE" in prompt


def test_prompt_payload_sizes_report_what_was_sent() -> None:
    sizes = prompt_payload_sizes(
        _statistics(metadata={"endpoint": [("/checkout", 9), ("/health", 1)]}),
        INCIDENT_TIMELINE,
    )

    assert sizes == {
        "milestones": 6,
        "buckets_total": 4,
        "buckets_sent": 4,
        "loggers": 2,
        "metadata_keys": 1,
    }


# ===========================================================================
# The deterministic fallback
# ===========================================================================


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(0.0, "info"), (0.049, "info"), (0.05, "warning"), (0.24, "warning"), (0.25, "critical"), (1.0, "critical")],
)
def test_severity_tiers_are_bounded_by_error_share(ratio: float, expected: str) -> None:
    assert severity_for_error_share(ratio) == expected


def test_fallback_reports_a_recovered_baseline_shift() -> None:
    result = build_fallback_summary(_statistics(), INCIDENT_TIMELINE)

    shift = next(a for a in result.anomalies if a.category == "baseline_shift")
    assert "2026-01-01T10:10:00+00:00" in shift.description
    assert "returned to it at 2026-01-01T10:30:00+00:00" in shift.description


def test_an_unrecovered_shift_is_escalated() -> None:
    # The window ended while the system was still degraded, which is worse than
    # the dataset-wide error share on its own suggests.
    timeline = [event for event in INCIDENT_TIMELINE if event.get("milestone_kind") != "recovery_onset"]

    result = build_fallback_summary(_statistics(error_ratio=0.1), timeline)

    shift = next(a for a in result.anomalies if a.category == "baseline_shift")
    assert shift.severity == "critical"
    assert "had not returned" in shift.description


def test_a_spike_is_reported_only_when_it_stands_above_the_series() -> None:
    spiky = build_fallback_summary(_statistics(), INCIDENT_TIMELINE)
    assert any(a.category == "volume_spike" for a in spiky.anomalies)

    # A flat series carrying the same errors in every bucket is a sustained
    # failure, not a spike — ``baseline_shift`` is the right report for it.
    flat = [
        _milestone("peak_error_volume", "2026-01-01T10:00:00+00:00"),
        _bucket("2026-01-01T10:00:00+00:00", errors=5),
        _bucket("2026-01-01T10:10:00+00:00", errors=5),
        _bucket("2026-01-01T10:20:00+00:00", errors=5),
    ]
    result = build_fallback_summary(_statistics(), flat)
    assert not any(a.category == "volume_spike" for a in result.anomalies)


def test_the_spike_threshold_is_the_documented_multiple() -> None:
    # mean of [0, 0, n] is n/3, so the peak clears SPIKE_RATIO * mean whenever
    # 3 > SPIKE_RATIO. Asserted against the constant rather than a literal so
    # the test tracks a retuning of the threshold.
    assert 3 > SPIKE_RATIO
    timeline = [
        _milestone("peak_error_volume", "2026-01-01T10:20:00+00:00"),
        _bucket("2026-01-01T10:00:00+00:00", errors=0),
        _bucket("2026-01-01T10:10:00+00:00", errors=0),
        _bucket("2026-01-01T10:20:00+00:00", errors=9),
    ]
    result = build_fallback_summary(_statistics(), timeline)
    spike = next(a for a in result.anomalies if a.category == "volume_spike")
    assert "peaked at 9" in spike.description
    assert "mean of 3.0" in spike.description


def test_a_cascade_needs_two_components_and_reports_their_order() -> None:
    result = build_fallback_summary(_statistics(), INCIDENT_TIMELINE)

    cascade = next(a for a in result.anomalies if a.category == "logger_cascade")
    assert "payment -> orders" in cascade.description
    # Reported as sequence, never as causation — the same line the system
    # prompt holds the model to.
    assert "does not establish it" in cascade.description
    assert result.cross_logger_correlations
    assert "no causal link was inferred" in result.cross_logger_correlations[0]


def test_a_single_component_is_not_a_cascade() -> None:
    timeline = [
        _bucket("2026-01-01T10:00:00+00:00", errors=3, loggers=["payment"]),
        _bucket("2026-01-01T10:10:00+00:00", errors=2, loggers=["payment"]),
    ]
    result = build_fallback_summary(_statistics(), timeline)

    assert not any(a.category == "logger_cascade" for a in result.anomalies)
    assert result.cross_logger_correlations == []


def test_metadata_concentration_is_reported_above_the_dominance_threshold() -> None:
    result = build_fallback_summary(
        _statistics(metadata={"endpoint": [("/checkout", 9), ("/health", 1)]}),
        INCIDENT_TIMELINE,
    )

    clustering = next(a for a in result.anomalies if a.category == "metadata_clustering")
    assert "'endpoint'" in clustering.description
    assert "9 of 10 records (90%)" in clustering.description
    assert result.metadata_insights == ["endpoint='/checkout' covers 90% of records (9/10)."]


def test_an_even_split_is_not_a_concentration() -> None:
    assert DOMINANCE_RATIO > 0.5
    result = build_fallback_summary(
        _statistics(metadata={"endpoint": [("/a", 5), ("/b", 5)]}),
        INCIDENT_TIMELINE,
    )
    assert not any(a.category == "metadata_clustering" for a in result.anomalies)
    assert result.metadata_insights == []


def test_a_constant_metadata_key_is_not_a_concentration() -> None:
    # Every record in a run carrying one ``service`` name says nothing about
    # where failures landed — it is a constant, not a cluster.
    result = build_fallback_summary(
        _statistics(metadata={"service": [("checkout", 10)]}),
        INCIDENT_TIMELINE,
    )
    assert not any(a.category == "metadata_clustering" for a in result.anomalies)


def test_the_fallback_is_well_formed_for_empty_inputs() -> None:
    result = build_fallback_summary(None, None)

    assert result.anomalies == []
    assert result.cross_logger_correlations == []
    assert result.metadata_insights == []
    assert result.behavioral_synthesis  # never blank; it says why it is empty
    assert "no model reasoning was available" in result.behavioral_synthesis


def test_the_fallback_is_deterministic() -> None:
    statistics = _statistics(metadata={"endpoint": [("/checkout", 9), ("/health", 1)]})
    first = build_fallback_summary(statistics, INCIDENT_TIMELINE)
    second = build_fallback_summary(statistics, INCIDENT_TIMELINE)

    assert first.model_dump() == second.model_dump()


def test_the_synthesis_restates_the_counts_it_was_given() -> None:
    result = build_fallback_summary(_statistics(error_count=4, warning_count=1), INCIDENT_TIMELINE)

    assert "4 error-level and 1 warning-level record(s)" in result.behavioral_synthesis
    assert "40.0%" in result.behavioral_synthesis
    assert "2026-01-01T10:00:00+00:00 to 2026-01-01T10:30:00+00:00" in result.behavioral_synthesis


# ===========================================================================
# Node execution
# ===========================================================================


def test_the_node_publishes_the_models_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _install_llm(monkeypatch, answer=RESULT)

    delta = pattern_analysis_node(
        {"statistics": _statistics(), "timeline": INCIDENT_TIMELINE}
    )

    assert delta["pattern_summary"] == RESULT.model_dump()
    assert delta["completed_stages"] == ["pattern_analysis"]
    # Nothing to report, so nothing is appended to the shared channel.
    assert "investigation_notes" not in delta
    assert "cross-logger cascades" in recorder.human_prompt


def test_the_node_sends_both_reports_and_the_upstream_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _install_llm(monkeypatch, answer=RESULT)

    pattern_analysis_node(
        {
            "application_name": "checkout-api",
            "statistics": _statistics(),
            "timeline": INCIDENT_TIMELINE,
            "investigation_notes": ["Data Quality Warning: 4 entries had no timestamp"],
        }
    )

    prompt = recorder.human_prompt
    assert "checkout-api" in prompt
    assert "STATISTICS" in prompt
    assert "error_onset" in prompt
    assert "4 entries had no timestamp" in prompt
    assert "site-reliability engineer" in recorder.system_prompt


def test_upstream_notes_are_read_but_never_written_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``investigation_notes`` uses an additive reducer, so echoing the input
    # back would duplicate every note the parser and timeline emitted.
    _install_llm(monkeypatch, answer=RESULT)

    delta = pattern_analysis_node(
        {
            "statistics": _statistics(),
            "timeline": INCIDENT_TIMELINE,
            "investigation_notes": ["parser: skipped 3 lines"],
        }
    )

    assert "parser: skipped 3 lines" not in delta.get("investigation_notes", [])


def test_a_dict_response_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``with_structured_output`` yields a plain dict on some provider and method
    # combinations; the delta must not vary with that.
    _install_llm(monkeypatch, answer=RESULT.model_dump())

    delta = pattern_analysis_node(
        {"statistics": _statistics(), "timeline": INCIDENT_TIMELINE}
    )

    assert delta["pattern_summary"] == RESULT.model_dump()


def test_the_provider_and_mode_come_from_state(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []

    def factory(provider: str, mode: str, **_: Any) -> Any:
        seen.append((provider, mode))
        yield "m", _FakeLLM(RESULT, _Recorder())

    monkeypatch.setattr(
        "graph_library.pattern_analysis.node.iter_error_analysis_llms", factory
    )

    pattern_analysis_node(
        {
            "statistics": _statistics(),
            "timeline": INCIDENT_TIMELINE,
            # Free text, as it arrives from a form field — normalized on the way
            # through, exactly as the error-analysis node normalizes it.
            "llm_provider": "Claude",
            "analysis_mode": " DEEP ",
        }
    )

    assert seen == [("anthropic", "deep")]


# -- degradation ------------------------------------------------------------


def test_an_empty_input_skips_the_call_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _install_llm(monkeypatch, answer=RESULT)

    delta = pattern_analysis_node({})

    # Spending a request to be told the input was empty helps nobody.
    assert recorder.calls == []
    assert delta["investigation_notes"] == [NO_INPUT_NOTE]
    assert delta["completed_stages"] == ["pattern_analysis"]
    assert delta["pattern_summary"] == build_fallback_summary(None, None).model_dump()


def test_a_missing_statistics_report_still_runs_on_the_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Half an input is still an input: the timeline alone supports the onset,
    # spike and cascade questions.
    recorder = _install_llm(monkeypatch, answer=RESULT)

    delta = pattern_analysis_node({"timeline": INCIDENT_TIMELINE})

    assert len(recorder.calls) == 1
    assert "unavailable — the statistics node produced nothing" in recorder.human_prompt
    assert delta["pattern_summary"] == RESULT.model_dump()


def test_a_missing_timeline_still_runs_on_the_statistics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _install_llm(monkeypatch, answer=RESULT)

    delta = pattern_analysis_node({"statistics": _statistics()})

    assert len(recorder.calls) == 1
    assert "could be placed on a time axis" in recorder.human_prompt
    assert delta["pattern_summary"] == RESULT.model_dump()


def test_a_raising_call_degrades_to_the_deterministic_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_llm(monkeypatch, answer=RuntimeError("OPENAI_API_KEY is not set"))

    delta = pattern_analysis_node(
        {"statistics": _statistics(), "timeline": INCIDENT_TIMELINE}
    )

    # The arithmetic findings are exactly as accurate as they were before the
    # call failed; only the interpretation is missing.
    assert delta["pattern_summary"] == build_fallback_summary(
        _statistics(), INCIDENT_TIMELINE
    ).model_dump()
    assert delta["completed_stages"] == ["pattern_analysis"]
    note = delta["investigation_notes"][0]
    assert "Pattern analysis: LLM reasoning unavailable" in note
    assert "OPENAI_API_KEY is not set" in note


def test_an_uninstalled_provider_degrades_rather_than_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def factory(*_args: Any, **_kwargs: Any) -> Any:
        raise ImportError("The 'anthropic' provider requires langchain-anthropic")
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(
        "graph_library.pattern_analysis.node.iter_error_analysis_llms", factory
    )

    delta = pattern_analysis_node(
        {"statistics": _statistics(), "timeline": INCIDENT_TIMELINE}
    )

    assert delta["pattern_summary"]["anomalies"]
    assert "langchain-anthropic" in delta["investigation_notes"][0]


def test_a_model_answering_in_prose_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``with_structured_output`` returns None rather than raising when the
    # schema was offered as an unforced tool call and the model ignored it.
    _install_llm(monkeypatch, answer=None)

    delta = pattern_analysis_node(
        {"statistics": _statistics(), "timeline": INCIDENT_TIMELINE}
    )

    assert "no structured response" in delta["investigation_notes"][0]
    assert delta["pattern_summary"]["behavioral_synthesis"]


def test_a_dead_model_falls_through_to_the_next_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _install_llm(
        monkeypatch,
        answer=[RuntimeError("404 model_not_found"), RESULT],
        models=["retired-model", "working-model"],
    )

    delta = pattern_analysis_node(
        {"statistics": _statistics(), "timeline": INCIDENT_TIMELINE}
    )

    assert len(recorder.calls) == 2
    assert delta["pattern_summary"] == RESULT.model_dump()
    # A silent substitution would make the report unreproducible.
    note = delta["investigation_notes"][0]
    assert "'retired-model' was unavailable" in note
    assert "'working-model' answered instead" in note


def test_a_credential_failure_is_not_retried_against_another_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Swapping the model would burn another request and fail the same way.
    recorder = _install_llm(
        monkeypatch,
        answer=[RuntimeError("401 invalid_api_key"), RESULT],
        models=["primary-model", "second-model"],
    )

    delta = pattern_analysis_node(
        {"statistics": _statistics(), "timeline": INCIDENT_TIMELINE}
    )

    assert len(recorder.calls) == 1
    assert "invalid_api_key" in delta["investigation_notes"][0]


def test_the_degraded_shape_matches_the_healthy_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A downstream consumer must never have to ask which path produced the
    # summary it is reading.
    _install_llm(monkeypatch, answer=RESULT)
    healthy = pattern_analysis_node(
        {"statistics": _statistics(), "timeline": INCIDENT_TIMELINE}
    )["pattern_summary"]

    _install_llm(monkeypatch, answer=RuntimeError("boom"))
    degraded = pattern_analysis_node(
        {"statistics": _statistics(), "timeline": INCIDENT_TIMELINE}
    )["pattern_summary"]

    assert set(healthy) == set(degraded) == set(PatternSummary.__annotations__)
    for key in healthy:
        assert type(healthy[key]) is type(degraded[key])


# ===========================================================================
# Graph integration
# ===========================================================================


#: Pino-shaped JSON with ISO timestamps, so the parser can stamp every entry
#: and the timeline node has a time axis to work with. Numeric epochs are
#: deliberately not used — the parser yields ``None`` for them, which would
#: leave the timeline empty and make these tests assert nothing.
RAW_LOGS = "\n".join(
    [
        '{"level":50,"time":"2026-01-01T10:00:00Z","name":"payment","msg":"Payment provider unreachable"}',
        '{"level":50,"time":"2026-01-01T10:00:01Z","name":"orders","msg":"Order 41 failed"}',
        '{"level":30,"time":"2026-01-01T10:00:02Z","name":"orders","msg":"Retry scheduled"}',
    ]
)


def _silence_error_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the sibling LLM node degrade instead of dialling a provider.

    These tests drive the whole graph, which runs ``error_analysis`` too. Left
    alone it would reach a real provider on any machine where ``OPENAI_API_KEY``
    happens to be set, so it is stubbed to fail rather than left to chance.
    """

    def factory(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("no provider is configured for this test")
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(
        "graph_library.error_analysis.node.iter_error_analysis_llms", factory
    )


def test_the_graph_run_populates_pattern_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_llm(monkeypatch, answer=RESULT)
    _silence_error_analysis(monkeypatch)

    final = compile_graph().invoke({"application_name": "checkout", "raw_logs": RAW_LOGS})

    assert final["pattern_summary"] == RESULT.model_dump()
    assert final["completed_stages"].count("pattern_analysis") == 1


def test_the_node_sees_the_real_statistics_and_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The point of running this through the graph rather than calling the node:
    # it proves the two upstream nodes' real output reaches the prompt in the
    # shape the formatter expects, which a hand-built fixture cannot.
    recorder = _install_llm(monkeypatch, answer=RESULT)
    _silence_error_analysis(monkeypatch)

    compile_graph().invoke({"application_name": "checkout", "raw_logs": RAW_LOGS})

    prompt = recorder.human_prompt
    assert "payment" in prompt
    assert "orders" in prompt
    assert "logs_start" in prompt
    assert "level_distribution" in prompt


def test_the_graph_run_degrades_when_no_model_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both LLM nodes fail and the investigation still completes, with
    # deterministic findings throughout.
    _install_llm(monkeypatch, answer=RuntimeError("no provider is configured"))
    _silence_error_analysis(monkeypatch)

    final = compile_graph().invoke({"application_name": "checkout", "raw_logs": RAW_LOGS})

    summary = final["pattern_summary"]
    assert set(summary) == set(PatternSummary.__annotations__)
    assert summary["behavioral_synthesis"]
    assert final["completed_stages"].count("pattern_analysis") == 1
    assert any(
        "Pattern analysis: LLM reasoning unavailable" in note
        for note in final["investigation_notes"]
    )

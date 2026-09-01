"""Tests for the Prepare Output Node (``graph_library/prepare_output/``).

Everything here runs offline. One seam is stubbed and nothing else:
``graph_library.prepare_output.node.iter_error_analysis_llms`` — a fake chat
model that records the prompt it was given and answers with whatever the test
told it to. Below that, the scoring engine and the prompt builder are driven
directly, because both are pure functions over the upstream payloads and
neither needs a model to be exercised.

The conventions asserted here:

    * the confidence score is deterministic arithmetic over parser health and
      the error analysis, and each of its four penalties is independently
      observable;
    * the prompt carries every upstream artifact, labelled by provenance, and
      states its own omissions;
    * the node degrades rather than fails — a raising call, a dead model or an
      empty input all still publish a complete ``structured_report``, with the
      reason in ``investigation_notes`` and the score discounted;
    * the degraded shape and the healthy shape are identical, so a downstream
      consumer never has to ask which one it got;
    * the published ``confidence_score`` is always the deterministic one, never
      the model's self-assessment.
"""

from __future__ import annotations

from typing import Any

import pytest

from graph import compile_graph
from graph_library import models
from graph_library.models import (
    ErrorSummary,
    ParserMetrics,
    PatternSummary,
    Statistics,
    TimelineEvent,
)
from graph_library.models.prepare_output import LLMPrepareOutputResult
from graph_library.prepare_output import (
    AMBIGUOUS_ROOT_CAUSE_PENALTY,
    BASE_SCORE,
    CONFIDENCE_DIVERGENCE_THRESHOLD,
    FALLBACK_EXECUTIVE_SUMMARY,
    FALLBACK_PENALTY,
    FALLBACK_ROOT_CAUSE,
    LOW_PARSER_CONFIDENCE_PENALTY,
    MAX_ERROR_SIGNATURES,
    MAX_HISTORICAL_INVESTIGATIONS,
    NO_INPUT_NOTE,
    apply_fallback_penalty,
    build_prepare_output_prompt,
    compute_confidence_score,
    confidence_breakdown,
    format_error_summary,
    format_historical_context,
    format_parser_health,
    format_pattern_summary,
    prepare_output_node,
    prompt_payload_sizes,
)
import graph_library.prepare_output.node as node_module
import graph_library.prepare_output.prompts as prompts_module
import graph_library.prepare_output.scoring as scoring_module

# ===========================================================================
# Payload builders
# ===========================================================================


def _parser_metrics(
    *,
    total_lines: int = 100,
    blank_lines: int = 0,
    parsed_lines: int = 100,
    malformed_lines: int = 0,
    missing_timestamp_lines: int = 0,
    parser_confidence: float = 1.0,
) -> ParserMetrics:
    """A ``ParserMetrics`` payload, healthy unless a test says otherwise."""
    return {
        "parser_name": "JSONLinesParser",
        "parser_confidence": parser_confidence,
        "detected_format": "json",
        "total_lines": total_lines,
        "blank_lines": blank_lines,
        "parsed_lines": parsed_lines,
        "malformed_lines": malformed_lines,
        "missing_timestamp_lines": missing_timestamp_lines,
    }


def _statistics() -> Statistics:
    return {
        "level_distribution": [
            {"value": "ERROR", "count": 4},
            {"value": "INFO", "count": 6},
        ],
        "logger_distribution": [
            {"value": "payment-client", "count": 3},
            {"value": "order-service", "count": 7},
        ],
        "severity": {
            "error_count": 4,
            "warning_count": 1,
            "error_ratio": 0.4,
            "warning_ratio": 0.1,
        },
        "timestamp_coverage": {
            "with_timestamp": 10,
            "without_timestamp": 0,
            "earliest": "2026-01-01T10:00:00+00:00",
            "latest": "2026-01-01T10:30:00+00:00",
        },
        "metadata_distributions": {
            "endpoint": [{"value": "/checkout", "count": 9}],
        },
    }


def _timeline() -> list[TimelineEvent]:
    return [
        {
            "event_type": "milestone",
            "timestamp": "2026-01-01T10:05:00+00:00",
            "end_timestamp": None,
            "milestone_kind": "first_error",
            "total_logs": 1,
            "error_count": 1,
            "warning_count": 0,
            "top_loggers": ["payment-client"],
            "sample_messages": ["connection refused"],
            "summary": "First error at 10:05.",
        },
        {
            "event_type": "bucket",
            "timestamp": "2026-01-01T10:10:00+00:00",
            "end_timestamp": "2026-01-01T10:15:00+00:00",
            "milestone_kind": None,
            "total_logs": 8,
            "error_count": 4,
            "warning_count": 1,
            "top_loggers": ["order-service"],
            "sample_messages": ["order 41 failed"],
            "summary": "4 error(s) in the window starting 10:10.",
        },
    ]


def _error_summary(
    *,
    primary: str | None = "ERR_001",
    signature_count: int = 2,
) -> ErrorSummary:
    return {
        "total_errors_analyzed": 12,
        "unique_signatures_found": signature_count,
        "primary_error_signature_id": primary,
        "signatures": [
            {
                "signature_id": f"ERR_{index + 1:03d}",
                "template": f"failure number <NUM> in component {index}",
                "severity": "ERROR",
                "count": 10 - index,
                "first_seen": "2026-01-01T10:05:00+00:00",
                "last_seen": "2026-01-01T10:20:00+00:00",
                "loggers": ["payment-client"],
                "sample_messages": ["connection refused to 10.0.0.4:5432"],
                "is_root_cause_candidate": index == 0,
                "explanation": "The payment client could not reach its database.",
            }
            for index in range(signature_count)
        ],
        "cascading_impact_summary": (
            "The payment failure propagated into the order service."
        ),
    }


def _pattern_summary() -> PatternSummary:
    return {
        "anomalies": [
            {
                "category": "logger_cascade",
                "severity": "critical",
                "description": "payment-client failed first, then order-service.",
                "affected_loggers": ["payment-client", "order-service"],
                "time_window": "2026-01-01T10:10:00+00:00",
            }
        ],
        "cross_logger_correlations": [
            "payment-client precedes order-service by one bucket."
        ],
        "metadata_insights": ["endpoint=/checkout carries most failures."],
        "behavioral_synthesis": "A payment outage propagated into orders.",
    }


def _state(**overrides: Any) -> dict[str, Any]:
    """A complete post-fan-in state, as the graph would present it."""
    state: dict[str, Any] = {
        "application_name": "checkout-api",
        "investigation_timestamp": "2026-01-01T11:00:00+00:00",
        "parser_metrics": _parser_metrics(),
        "statistics": _statistics(),
        "timeline": _timeline(),
        "error_summary": _error_summary(),
        "pattern_summary": _pattern_summary(),
        "investigation_notes": ["Parser: 0 malformed lines."],
    }
    state.update(overrides)
    return state


RESULT = LLMPrepareOutputResult(
    root_cause=(
        "The payment client lost its database connection, which stalled every "
        "downstream order."
    ),
    executive_summary=(
        "At 10:05 the payment client began refusing connections.\n\n"
        "Within one bucket the failure had spread to the order service.\n\n"
        "Every line in the payload parsed cleanly, so this conclusion carries "
        "no ingestion caveat."
    ),
    llm_confidence_score=90,
)


# ===========================================================================
# The fake model
# ===========================================================================


class _Recorder:
    """Captures every structured call the node made."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    @property
    def system_prompt(self) -> str:
        return self.calls[-1][1]

    @property
    def human_prompt(self) -> str:
        return self.calls[-1][2]


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
    models_yielded: list[str] | None = None,
) -> _Recorder:
    """Point the prepare-output node at a fake model.

    Args:
        answer: What the call should return — an ``LLMPrepareOutputResult``, a
            plain dict, ``None``, or an exception for it to raise. A list of
            answers is consumed one per candidate model.
        models_yielded: The candidate ids the factory should yield, best first.
    """
    recorder = _Recorder()
    answers = list(answer) if isinstance(answer, list) else None

    def factory(*_args: Any, **_kwargs: Any) -> Any:
        for index, model in enumerate(models_yielded or ["primary-model"]):
            reply = answers[index] if answers else answer
            yield model, _FakeLLM(reply, recorder)

    monkeypatch.setattr(
        "graph_library.prepare_output.node.iter_error_analysis_llms", factory
    )
    return recorder


# ===========================================================================
# The deterministic confidence engine
# ===========================================================================


def test_a_clean_investigation_scores_the_base_score() -> None:
    assert compute_confidence_score(_state()) == BASE_SCORE


def test_confidence_score_penalties() -> None:
    # Each penalty in isolation, so a weight change shows up as one failing
    # assertion rather than as a single wrong total.

    # 10 of 100 lines malformed -> 10% -> 1 point per percent.
    malformed = _state(
        parser_metrics=_parser_metrics(total_lines=100, malformed_lines=10, parsed_lines=90)
    )
    assert compute_confidence_score(malformed) == BASE_SCORE - 10

    # 20 of 100 parsed entries carry no timestamp -> 20% -> 1 point per 2%.
    missing_timestamps = _state(
        parser_metrics=_parser_metrics(missing_timestamp_lines=20)
    )
    assert compute_confidence_score(missing_timestamps) == BASE_SCORE - 10

    # No primary signature nominated.
    ambiguous = _state(error_summary=_error_summary(primary=None))
    assert compute_confidence_score(ambiguous) == BASE_SCORE - AMBIGUOUS_ROOT_CAUSE_PENALTY

    # Format detection was itself a guess.
    low_confidence = _state(parser_metrics=_parser_metrics(parser_confidence=0.5))
    assert (
        compute_confidence_score(low_confidence)
        == BASE_SCORE - LOW_PARSER_CONFIDENCE_PENALTY
    )

    # And all four together, which is the case a single-penalty test cannot
    # catch: the deductions accumulate rather than saturating at the largest.
    everything = _state(
        parser_metrics=_parser_metrics(
            total_lines=100,
            parsed_lines=80,
            malformed_lines=20,
            missing_timestamp_lines=40,
            parser_confidence=0.4,
        ),
        error_summary=_error_summary(primary=None),
    )
    # -20 malformed (20%), -25 missing timestamps (40/80 = 50%), -15, -10.
    assert compute_confidence_score(everything) == BASE_SCORE - 20 - 25 - 15 - 10


def test_the_penalty_boundary_is_inclusive_at_the_confidence_floor() -> None:
    # 0.80 exactly is "good enough" — the penalty applies strictly below it.
    at_floor = _state(parser_metrics=_parser_metrics(parser_confidence=0.80))
    just_below = _state(parser_metrics=_parser_metrics(parser_confidence=0.79))

    assert compute_confidence_score(at_floor) == BASE_SCORE
    assert (
        compute_confidence_score(just_below)
        == BASE_SCORE - LOW_PARSER_CONFIDENCE_PENALTY
    )


def test_a_zero_parser_confidence_is_penalized_rather_than_read_as_missing() -> None:
    # ``.get(key) or 1.0`` would read 0.0 as "not reported" and skip the
    # penalty on the least trustworthy detection there is.
    unusable = _state(parser_metrics=_parser_metrics(parser_confidence=0.0))
    assert (
        compute_confidence_score(unusable)
        == BASE_SCORE - LOW_PARSER_CONFIDENCE_PENALTY
    )


def test_an_absent_parser_confidence_is_not_penalized() -> None:
    metrics = _parser_metrics()
    del metrics["parser_confidence"]  # type: ignore[misc]
    assert compute_confidence_score(_state(parser_metrics=metrics)) == BASE_SCORE

    metrics_with_none = {**_parser_metrics(), "parser_confidence": None}
    assert compute_confidence_score(_state(parser_metrics=metrics_with_none)) == (
        BASE_SCORE
    )


def test_the_score_is_clamped_to_the_published_scale() -> None:
    # Every line malformed, nothing timestamped, no root cause, no confidence:
    # the raw arithmetic goes below zero and the published score does not.
    unusable = _state(
        parser_metrics=_parser_metrics(
            total_lines=100,
            parsed_lines=100,
            malformed_lines=100,
            missing_timestamp_lines=100,
            parser_confidence=0.1,
        ),
        error_summary=_error_summary(primary=None),
    )
    assert compute_confidence_score(unusable) == 0


def test_an_empty_state_never_divides_by_zero() -> None:
    # A totally empty payload is measurable-as-nothing, not maximally bad: the
    # ratio penalties cannot be formed, and the missing root cause is what
    # carries the discount.
    assert compute_confidence_score({}) == BASE_SCORE - AMBIGUOUS_ROOT_CAUSE_PENALTY


def test_the_breakdown_names_every_penalty_even_when_it_did_not_fire() -> None:
    breakdown = confidence_breakdown(_state())
    assert set(breakdown) == {
        "malformed_lines",
        "missing_timestamps",
        "ambiguous_root_cause",
        "low_parser_confidence",
    }
    assert all(points == 0.0 for points in breakdown.values())


def test_rounding_is_deferred_to_the_final_score() -> None:
    # Two sub-point penalties must cost a point together rather than nothing
    # each: 1 of 300 malformed is 0.33 points, 2 of 300 missing timestamps is
    # 0.33 points, and rounding either alone would discard it.
    state = _state(
        parser_metrics=_parser_metrics(
            total_lines=300, parsed_lines=299, malformed_lines=1, missing_timestamp_lines=2
        )
    )
    breakdown = confidence_breakdown(state)
    assert 0 < breakdown["malformed_lines"] < 1
    assert 0 < breakdown["missing_timestamps"] < 1
    assert compute_confidence_score(state) == BASE_SCORE - 1


def test_the_fallback_penalty_discounts_and_clamps() -> None:
    assert apply_fallback_penalty(100) == 100 - FALLBACK_PENALTY
    assert apply_fallback_penalty(FALLBACK_PENALTY) == 0
    assert apply_fallback_penalty(0) == 0


# ===========================================================================
# Prompt assembly
# ===========================================================================


def test_the_prompt_carries_every_upstream_artifact() -> None:
    prompt = build_prepare_output_prompt(
        _parser_metrics(),
        _statistics(),
        _timeline(),
        _error_summary(),
        _pattern_summary(),
        ["Parser: 3 malformed lines were skipped."],
        [{"root_cause": "a previous outage", "confidence_score": 80}],
        application_name="checkout-api",
        investigation_timestamp="2026-01-01T11:00:00+00:00",
    )

    # Every section is present and labelled.
    for heading in (
        "PARSER HEALTH",
        "STATISTICS",
        "TIMELINE",
        "ERROR ANALYSIS",
        "PATTERN ANALYSIS",
        "INVESTIGATION NOTES",
        "HISTORICAL CONTEXT",
    ):
        assert heading in prompt

    # And the payloads behind them.
    assert "checkout-api" in prompt
    assert "2026-01-01T11:00:00+00:00" in prompt
    assert "first_error" in prompt
    assert "ERR_001" in prompt
    assert "logger_cascade" in prompt
    assert "3 malformed lines were skipped" in prompt
    assert "a previous outage" in prompt


def test_measurement_is_ordered_before_inference() -> None:
    # The system prompt asks the model to disagree with the upstream models
    # where the numbers do not support them, which it can only do if it reads
    # the numbers first.
    prompt = build_prepare_output_prompt(
        _parser_metrics(),
        _statistics(),
        _timeline(),
        _error_summary(),
        _pattern_summary(),
    )
    assert prompt.index("PARSER HEALTH") < prompt.index("STATISTICS")
    assert prompt.index("STATISTICS") < prompt.index("TIMELINE")
    assert prompt.index("TIMELINE") < prompt.index("ERROR ANALYSIS")
    assert prompt.index("ERROR ANALYSIS") < prompt.index("PATTERN ANALYSIS")


def test_the_two_model_derived_sections_are_labelled_as_inference() -> None:
    # A synthesis that treats another model's nomination as a measured fact
    # compounds the first model's error.
    assert "another model's conclusions" in format_error_summary(_error_summary())
    assert "another model's conclusions" in format_pattern_summary(_pattern_summary())


def test_the_deterministic_score_is_withheld_from_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The model is asked to rate signal clarity independently; handing it the
    # deterministic answer would collapse two readings into one.
    recorder = _install_llm(monkeypatch, answer=RESULT)
    state = _state(parser_metrics=_parser_metrics(total_lines=100, malformed_lines=37, parsed_lines=63))

    delta = prepare_output_node(state)

    assert delta["confidence_score"] == BASE_SCORE - 37
    assert "63/100" not in recorder.human_prompt
    # The raw metrics it needs for the caveat are there, though.
    assert "malformed_lines" in recorder.human_prompt
    assert "37" in recorder.human_prompt


def test_signatures_are_capped_and_the_omission_is_stated() -> None:
    many = _error_summary(signature_count=MAX_ERROR_SIGNATURES + 5)
    rendered = format_error_summary(many)

    assert f"ERR_{MAX_ERROR_SIGNATURES:03d}" in rendered
    assert f"ERR_{MAX_ERROR_SIGNATURES + 1:03d}" not in rendered
    assert "5 lowest-volume signature(s) were omitted" in rendered


def test_sample_messages_are_not_resent_with_the_signatures() -> None:
    # The error-analysis node has already read them and published an
    # explanation per signature; resending is the largest avoidable cost here.
    rendered = format_error_summary(_error_summary())
    assert "connection refused to 10.0.0.4:5432" not in rendered
    assert "could not reach its database" in rendered


def test_historical_context_is_capped_and_the_omission_is_stated() -> None:
    history = [{"root_cause": f"outage {index}"} for index in range(6)]
    rendered = format_historical_context(history)

    assert "outage 0" in rendered
    assert f"outage {MAX_HISTORICAL_INVESTIGATIONS}" not in rendered
    assert (
        f"and {6 - MAX_HISTORICAL_INVESTIGATIONS} older investigation(s), omitted"
        in rendered
    )


def test_a_long_historical_entry_is_truncated() -> None:
    rendered = format_historical_context([{"summary": "x" * 5000}])
    assert "(truncated)" in rendered
    assert len(rendered) < 5000


def test_missing_sections_say_so_rather_than_rendering_empty_objects() -> None:
    # "We do not know how much was readable" is itself a caveat the summary
    # should carry, and an empty JSON object does not communicate it.
    assert "unavailable" in format_parser_health(None)
    assert "unavailable" in format_parser_health({})
    assert "unavailable" in format_error_summary(None)
    assert "unavailable" in format_pattern_summary(None)
    # A first-ever investigation is normal, not a degradation.
    assert "(none" in format_historical_context(None)


def test_the_prompt_is_well_formed_on_an_entirely_empty_payload() -> None:
    prompt = build_prepare_output_prompt(None, None, None, None, None, None, None)
    assert "PARSER HEALTH" in prompt
    assert "HISTORICAL CONTEXT" in prompt


def test_payload_sizes_report_what_was_sent_and_what_was_dropped() -> None:
    sizes = prompt_payload_sizes(
        _statistics(),
        _timeline(),
        _error_summary(signature_count=MAX_ERROR_SIGNATURES + 3),
        _pattern_summary(),
        [{"a": 1}] * 5,
    )
    assert sizes["signatures_total"] == MAX_ERROR_SIGNATURES + 3
    assert sizes["signatures_sent"] == MAX_ERROR_SIGNATURES
    assert sizes["milestones"] == 1
    assert sizes["anomalies"] == 1
    assert sizes["historical_total"] == 5
    assert sizes["historical_sent"] == MAX_HISTORICAL_INVESTIGATIONS


# ===========================================================================
# The node — the healthy path
# ===========================================================================


def test_prepare_output_node_success(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _install_llm(monkeypatch, answer=RESULT)
    state = _state()

    delta = prepare_output_node(state)

    # The synthesis reaches the flat output fields.
    assert delta["root_cause"] == RESULT.root_cause
    assert delta["executive_summary"] == RESULT.executive_summary
    assert delta["confidence_score"] == BASE_SCORE
    assert delta["completed_stages"] == ["prepare_output"]
    # Nothing to report, so nothing is appended to the shared channel.
    assert "investigation_notes" not in delta

    report = delta["structured_report"]
    assert set(report) == {
        "metadata",
        "synthesis",
        "deterministic_outputs",
        "ai_insights",
    }

    assert report["metadata"] == {
        "application_name": "checkout-api",
        "investigation_timestamp": "2026-01-01T11:00:00+00:00",
        "analysis_mode": "standard",
        "llm_provider": "openai",
        "confidence_score": BASE_SCORE,
        "parser_metrics": _parser_metrics(),
    }
    assert report["synthesis"] == {
        "root_cause": RESULT.root_cause,
        "executive_summary": RESULT.executive_summary,
        "investigation_notes": ["Parser: 0 malformed lines."],
    }
    # Upstream artifacts are carried verbatim — a UI that wants to draw the
    # timeline needs the timeline, not a sentence about it.
    assert report["deterministic_outputs"]["statistics"] == _statistics()
    assert report["deterministic_outputs"]["timeline"] == _timeline()
    assert report["ai_insights"]["error_summary"] == _error_summary()
    assert report["ai_insights"]["pattern_summary"] == _pattern_summary()

    # One call, against the documented schema.
    assert [schema for schema, _system, _human in recorder.calls] == [
        "LLMPrepareOutputResult"
    ]
    assert "incident commander" in recorder.system_prompt


def test_the_node_reads_the_state_without_mutating_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_llm(monkeypatch, answer=RESULT)
    state = _state()
    before = {key: repr(value) for key, value in state.items()}

    prepare_output_node(state)

    assert {key: repr(value) for key, value in state.items()} == before


def test_upstream_notes_are_snapshotted_but_never_written_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``investigation_notes`` uses an additive reducer, so echoing the input
    # back would duplicate every note the upstream passes emitted.
    _install_llm(monkeypatch, answer=RESULT)
    upstream = ["Parser: 3 malformed lines.", "Timeline: 2 empty windows dropped."]

    delta = prepare_output_node(_state(investigation_notes=upstream))

    assert "investigation_notes" not in delta
    assert delta["structured_report"]["synthesis"]["investigation_notes"] == upstream


def test_the_report_snapshot_does_not_alias_the_state_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The additive reducer appends to the state's own list; a snapshot that
    # aliased it would keep growing after the report was built.
    _install_llm(monkeypatch, answer=RESULT)
    notes = ["Parser: 3 malformed lines."]

    delta = prepare_output_node(_state(investigation_notes=notes))
    notes.append("appended later")

    assert delta["structured_report"]["synthesis"]["investigation_notes"] == [
        "Parser: 3 malformed lines."
    ]


def test_a_plain_dict_response_is_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``with_structured_output`` yields a dict on some provider/method
    # combinations; the delta must have one shape regardless.
    _install_llm(
        monkeypatch,
        answer={
            "root_cause": "a dict came back",
            "executive_summary": "several paragraphs",
            "llm_confidence_score": 88,
        },
    )

    delta = prepare_output_node(_state())

    assert delta["root_cause"] == "a dict came back"
    assert delta["confidence_score"] == BASE_SCORE


def test_a_wrapped_tool_argument_payload_is_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The deviation ``_ToolArgumentEnvelope`` exists to repair: some providers
    # nest the tool-call arguments one level deeper than the schema.
    _install_llm(
        monkeypatch,
        answer={
            "content": {
                "root_cause": "unwrapped correctly",
                "executive_summary": "several paragraphs",
                "llm_confidence_score": 70,
            }
        },
    )

    delta = prepare_output_node(_state())

    assert delta["root_cause"] == "unwrapped correctly"


def test_the_provider_and_mode_recorded_are_the_ones_actually_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The metadata block is the run's reproducibility record: "anthropic" is
    # what answered when the caller typed "Claude".
    _install_llm(monkeypatch, answer=RESULT)

    delta = prepare_output_node(_state(llm_provider="Claude", analysis_mode="DEEP"))

    assert delta["structured_report"]["metadata"]["llm_provider"] == "anthropic"
    assert delta["structured_report"]["metadata"]["analysis_mode"] == "deep"


def test_a_substituted_model_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    # A silent substitution would make the report unreproducible.
    dead = RuntimeError("404 model_not_found")
    _install_llm(
        monkeypatch,
        answer=[dead, RESULT],
        models_yielded=["retired-model", "working-model"],
    )

    delta = prepare_output_node(_state())

    assert delta["root_cause"] == RESULT.root_cause
    assert any(
        "'retired-model' was unavailable" in note
        for note in delta["investigation_notes"]
    )


def test_a_credential_failure_is_not_retried_against_another_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Swapping the model would burn another request and fail the same way.
    recorder = _install_llm(
        monkeypatch,
        answer=RuntimeError("401 invalid_api_key"),
        models_yielded=["primary-model", "second-model"],
    )

    delta = prepare_output_node(_state())

    assert len(recorder.calls) == 1
    assert delta["root_cause"] == FALLBACK_ROOT_CAUSE


# ===========================================================================
# The node — the published confidence score
# ===========================================================================


def test_the_published_score_is_deterministic_not_the_models_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model rating a payload it cannot see the gaps in must not be able to
    # raise the score above what the evidence supports.
    _install_llm(
        monkeypatch,
        answer=LLMPrepareOutputResult(
            root_cause="the payment client",
            executive_summary="several paragraphs",
            llm_confidence_score=100,
        ),
    )
    state = _state(
        parser_metrics=_parser_metrics(
            total_lines=100, parsed_lines=70, malformed_lines=30
        )
    )

    delta = prepare_output_node(state)

    assert delta["confidence_score"] == BASE_SCORE - 30
    assert delta["structured_report"]["metadata"]["confidence_score"] == BASE_SCORE - 30


def test_a_wide_confidence_gap_is_recorded_as_a_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_llm(
        monkeypatch,
        answer=LLMPrepareOutputResult(
            root_cause="the payment client",
            executive_summary="several paragraphs",
            llm_confidence_score=95,
        ),
    )
    # 40 of 100 lines malformed -> deterministic 60 against the model's 95.
    state = _state(
        parser_metrics=_parser_metrics(
            total_lines=100, parsed_lines=60, malformed_lines=40
        )
    )

    delta = prepare_output_node(state)

    assert delta["confidence_score"] == 60
    assert any(
        "rated its own confidence at 95/100" in note
        for note in delta["investigation_notes"]
    )


def test_a_narrow_confidence_gap_is_left_unremarked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The two scores measure different things, so a gap is expected and only a
    # wide one is informative.
    _install_llm(
        monkeypatch,
        answer=LLMPrepareOutputResult(
            root_cause="the payment client",
            executive_summary="several paragraphs",
            llm_confidence_score=BASE_SCORE - CONFIDENCE_DIVERGENCE_THRESHOLD,
        ),
    )

    delta = prepare_output_node(_state())

    assert "investigation_notes" not in delta


# ===========================================================================
# The node — the degraded paths
# ===========================================================================


def test_prepare_output_node_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_llm(monkeypatch, answer=RuntimeError("connection reset by peer"))
    state = _state()

    delta = prepare_output_node(state)

    # Fixed fallback text, unmistakably an absence rather than a finding.
    assert delta["root_cause"] == FALLBACK_ROOT_CAUSE
    assert delta["executive_summary"] == FALLBACK_EXECUTIVE_SUMMARY
    # Discounted, because a generic summary should not read as confidently as
    # a reasoned one.
    assert delta["confidence_score"] == BASE_SCORE - FALLBACK_PENALTY
    assert delta["completed_stages"] == ["prepare_output"]

    # The reason is recorded rather than left silent.
    assert any(
        "LLM synthesis unavailable" in note and "connection reset by peer" in note
        for note in delta["investigation_notes"]
    )

    # And the report is complete: every deterministic number survives.
    report = delta["structured_report"]
    assert set(report) == {
        "metadata",
        "synthesis",
        "deterministic_outputs",
        "ai_insights",
    }
    assert report["metadata"]["confidence_score"] == BASE_SCORE - FALLBACK_PENALTY
    assert report["deterministic_outputs"]["statistics"] == _statistics()
    assert report["deterministic_outputs"]["timeline"] == _timeline()
    assert report["ai_insights"]["error_summary"] == _error_summary()
    assert report["ai_insights"]["pattern_summary"] == _pattern_summary()
    assert report["synthesis"]["root_cause"] == FALLBACK_ROOT_CAUSE


def test_the_degraded_shape_is_identical_to_the_healthy_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A consumer must never have to ask which one it received.
    _install_llm(monkeypatch, answer=RESULT)
    healthy = prepare_output_node(_state())

    _install_llm(monkeypatch, answer=RuntimeError("no"))
    degraded = prepare_output_node(_state())

    assert set(healthy) | {"investigation_notes"} == set(degraded)
    assert healthy["structured_report"].keys() == degraded["structured_report"].keys()
    for section in healthy["structured_report"]:
        assert (
            healthy["structured_report"][section].keys()
            == degraded["structured_report"][section].keys()
        )


@pytest.mark.parametrize(
    "answer",
    [None, RuntimeError("boom"), {"unexpected": "shape"}],
    ids=["prose-instead-of-a-tool-call", "raising-call", "invalid-payload"],
)
def test_every_model_failure_mode_still_publishes_a_report(
    monkeypatch: pytest.MonkeyPatch, answer: Any
) -> None:
    _install_llm(monkeypatch, answer=answer)

    delta = prepare_output_node(_state())

    assert delta["root_cause"] == FALLBACK_ROOT_CAUSE
    assert delta["confidence_score"] == BASE_SCORE - FALLBACK_PENALTY
    assert delta["structured_report"]["deterministic_outputs"]["statistics"] == (
        _statistics()
    )
    assert delta["investigation_notes"]


def test_an_absent_provider_package_degrades_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def factory(*_args: Any, **_kwargs: Any) -> Any:
        raise ImportError("The 'anthropic' provider requires langchain-anthropic")

    monkeypatch.setattr(
        "graph_library.prepare_output.node.iter_error_analysis_llms", factory
    )

    delta = prepare_output_node(_state())

    assert delta["root_cause"] == FALLBACK_ROOT_CAUSE
    assert any("ImportError" in note for note in delta["investigation_notes"])


def test_an_empty_investigation_skips_the_call_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Calling a model here would spend a request to be told the input was
    # empty; the report is still published so the run has an artifact.
    recorder = _install_llm(monkeypatch, answer=RESULT)

    delta = prepare_output_node(
        {"application_name": "quiet-app", "parser_metrics": _parser_metrics()}
    )

    assert recorder.calls == []
    assert delta["investigation_notes"] == [NO_INPUT_NOTE]
    assert delta["root_cause"] == FALLBACK_ROOT_CAUSE
    assert delta["structured_report"]["metadata"]["application_name"] == "quiet-app"


def test_a_partial_state_still_produces_a_complete_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fan-in guarantees every producer ran, but the report is the thing
    # that gets stored: a KeyError here would lose an otherwise complete
    # investigation.
    _install_llm(monkeypatch, answer=RESULT)

    delta = prepare_output_node({"statistics": _statistics()})

    report = delta["structured_report"]
    assert report["metadata"]["application_name"] == "unknown"
    assert report["metadata"]["investigation_timestamp"] == ""
    assert report["metadata"]["parser_metrics"] == {}
    assert report["deterministic_outputs"]["timeline"] == []
    assert report["ai_insights"]["error_summary"] == {}
    assert report["synthesis"]["investigation_notes"] == []


# ===========================================================================
# Architecture and wiring
# ===========================================================================


def test_the_report_model_is_defined_once_in_the_models_package() -> None:
    # The same single-source-of-truth rule ``tests/test_models_architecture.py``
    # enforces for every other shared model.
    assert (
        models.StructuredInvestigationReport
        is node_module.StructuredInvestigationReport
    )
    import graph

    assert graph.StructuredInvestigationReport is (
        models.StructuredInvestigationReport
    )


def test_the_package_imports_shared_models_rather_than_redefining_them() -> None:
    assert prompts_module.Statistics is models.Statistics
    assert prompts_module.TimelineEvent is models.TimelineEvent
    assert prompts_module.ErrorSummary is models.ErrorSummary
    assert prompts_module.PatternSummary is models.PatternSummary
    assert prompts_module.ParserMetrics is models.ParserMetrics


def test_the_report_is_a_typeddict_with_four_provenance_sections() -> None:
    assert hasattr(models.StructuredInvestigationReport, "__required_keys__")
    assert set(models.StructuredInvestigationReport.__annotations__) == {
        "metadata",
        "synthesis",
        "deterministic_outputs",
        "ai_insights",
    }


def test_the_llm_schema_matches_the_documented_contract() -> None:
    assert set(LLMPrepareOutputResult.model_fields) == {
        "root_cause",
        "executive_summary",
        "llm_confidence_score",
    }
    # Every field description is part of the prompt the provider sees.
    for field in LLMPrepareOutputResult.model_fields.values():
        assert field.description


def test_the_node_is_registered_with_defer_and_the_topology_is_unchanged() -> None:
    # ``defer`` is what keeps the fan-in a fan-in: without it the join fires as
    # soon as the plain edges have been written and this node runs twice.
    from graph import build_graph

    assert build_graph().nodes["prepare_output"].defer is True
    assert compile_graph().get_graph().nodes.keys() >= {
        "prepare_output",
        "write_to_db",
    }


def test_the_node_runs_once_in_a_real_graph_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Observed through ``stream`` rather than the final state: the frozen
    # ``write_to_db`` stub writes ``structured_report: {}`` after this node
    # runs, so the final state does not preserve what this node published.
    monkeypatch.setattr(
        "graph_library.prepare_output.node.iter_error_analysis_llms",
        lambda *_a, **_k: iter([("primary-model", _FakeLLM(RESULT, _Recorder()))]),
    )
    raw_logs = "\n".join(
        [
            "2026-01-01T00:00:00Z ERROR order-service payment provider unreachable",
            "2026-01-01T00:00:01Z INFO order-service retrying",
            "2026-01-01T00:00:02Z ERROR order-service order 41 failed",
        ]
    )

    deltas = [
        update["prepare_output"]
        for update in compile_graph().stream(
            {"application_name": "orders", "raw_logs": raw_logs},
            stream_mode="updates",
        )
        if "prepare_output" in update
    ]

    assert len(deltas) == 1
    delta = deltas[0]
    assert delta["root_cause"] == RESULT.root_cause
    assert delta["completed_stages"] == ["prepare_output"]
    # The parser really ran, and its metrics really reached the report through
    # the direct ``parser -> prepare_output`` edge.
    assert delta["structured_report"]["metadata"]["parser_metrics"]["total_lines"] == 3
    assert delta["structured_report"]["deterministic_outputs"]["timeline"]


def test_the_scoring_module_holds_every_weight_at_module_scope() -> None:
    # The penalty policy is meant to be readable and tunable in one place.
    for name in (
        "BASE_SCORE",
        "MALFORMED_PENALTY_PER_PERCENT",
        "MISSING_TIMESTAMP_PERCENT_PER_POINT",
        "AMBIGUOUS_ROOT_CAUSE_PENALTY",
        "LOW_PARSER_CONFIDENCE_PENALTY",
        "MIN_PARSER_CONFIDENCE",
        "FALLBACK_PENALTY",
    ):
        assert isinstance(getattr(scoring_module, name), (int, float))

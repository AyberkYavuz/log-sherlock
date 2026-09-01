"""The deterministic confidence-scoring engine for the Prepare Output Node.

The score answers *"how much should a reader trust the conclusion above it?"*,
and it is computed by arithmetic rather than asked of the model on purpose. A
model asked to rate its own confidence rates the *clarity of the signal it was
shown* — it has no way to know that a third of the payload never reached it. The
four penalties below are exactly the things a model cannot see:

    * lines the parser could not read at all,
    * entries that carried no timestamp, so no ordering claim covers them,
    * an error analysis that could not single out a primary signature,
    * a format detection that was itself a guess.

Every penalty is subtractive and bounded, so the score degrades smoothly rather
than falling off a cliff, and the same investigation always scores the same.
The node applies one further discount — :data:`FALLBACK_PENALTY`, via
:func:`apply_fallback_penalty` — when its own synthesis pass could not run.

The weights are policy, not arithmetic truth, which is why they are named
constants at module scope: tuning them is a one-line edit and a reader can see
the whole policy at a glance.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Where every investigation starts. A clean parse of a well-formed payload
#: whose root cause was identified scores exactly this.
BASE_SCORE = 100

#: Bounds of the published scale. Expressed on 0-100 integers rather than
#: 0.0-1.0 because a whole-number percentage is what a report shows a human.
MIN_SCORE = 0
MAX_SCORE = 100

#: Points deducted per 1% of non-blank lines the parser could not parse.
#: Weighted highest of the ratio penalties: a malformed line contributed
#: *nothing* to any downstream analysis, so it is pure missing evidence.
MALFORMED_PENALTY_PER_PERCENT = 1.0

#: How many percent of timestamp-less entries cost one point. Half the weight
#: of a malformed line, and deliberately so: an entry with no timestamp still
#: reached the statistics and the error fingerprinting, it only dropped out of
#: the timeline. The evidence is partial rather than absent.
MISSING_TIMESTAMP_PERCENT_PER_POINT = 2.0

#: Deducted when the error analysis nominated no primary signature. The single
#: largest penalty, because a root-cause statement with no root-cause candidate
#: behind it is the one output most likely to read more certain than it is.
AMBIGUOUS_ROOT_CAUSE_PENALTY = 15

#: Deducted when format detection scored below :data:`MIN_PARSER_CONFIDENCE`.
#: A low-confidence detection means the fields every downstream node read may
#: have been extracted by an approximately-right pattern.
LOW_PARSER_CONFIDENCE_PENALTY = 10

#: The detection confidence at or above which no penalty applies.
MIN_PARSER_CONFIDENCE = 0.80

#: Deducted by :func:`apply_fallback_penalty` when the synthesis LLM pass did
#: not produce an answer. The report is still complete and every deterministic
#: number in it is still exact; what is missing is the reasoning that connects
#: them, and a generic summary should not carry the same confidence as a
#: reasoned one.
FALLBACK_PENALTY = 10


def _ratio_percent(numerator: Any, denominator: Any) -> float:
    """``numerator / denominator`` as a percentage, safe on junk input.

    Returns ``0.0`` whenever the ratio cannot be formed — a zero, missing or
    non-numeric denominator. That direction is deliberate: an unmeasurable
    payload gets no penalty rather than a maximal one, because "we could not
    tell" is not evidence of poor quality. The absent-input case is already
    covered by :data:`AMBIGUOUS_ROOT_CAUSE_PENALTY`, which fires on exactly the
    empty analysis such a payload produces.
    """
    try:
        total = float(denominator or 0)
        part = float(numerator or 0)
    except (TypeError, ValueError):
        return 0.0

    if total <= 0 or part <= 0:
        return 0.0

    return part / total * 100.0


def confidence_breakdown(state: dict[str, Any]) -> dict[str, float]:
    """Every penalty this state incurs, keyed by name.

    Exposed alongside :func:`compute_confidence_score` so a run is diagnosable:
    a report that scored 61 raises "why?", and a single number cannot answer it.
    The node logs this mapping at ``INFO``.

    Args:
        state: The LogSherlock graph state. Reads ``parser_metrics`` and
            ``error_summary``; both are treated as read-only and both may be
            absent.

    Returns:
        A mapping of penalty name to points deducted, every value ``>= 0.0``.
        Keys are always present, so a caller can render the full policy
        including the penalties that did not fire.
    """
    parser_metrics = state.get("parser_metrics") or {}
    error_summary = state.get("error_summary") or {}

    malformed_percent = _ratio_percent(
        parser_metrics.get("malformed_lines"), parser_metrics.get("total_lines")
    )
    missing_timestamp_percent = _ratio_percent(
        parser_metrics.get("missing_timestamp_lines"),
        parser_metrics.get("parsed_lines"),
    )

    # ``.get(key, default)`` rather than ``.get(key) or default``: a genuine
    # ``0.0`` confidence must keep its penalty, and ``0.0 or 1.0`` is ``1.0``.
    # An explicit ``None`` is treated as "not reported" and so as no penalty,
    # matching the missing-key case.
    parser_confidence = parser_metrics.get("parser_confidence", 1.0)
    if parser_confidence is None:
        parser_confidence = 1.0

    return {
        "malformed_lines": malformed_percent * MALFORMED_PENALTY_PER_PERCENT,
        "missing_timestamps": (
            missing_timestamp_percent / MISSING_TIMESTAMP_PERCENT_PER_POINT
        ),
        "ambiguous_root_cause": (
            float(AMBIGUOUS_ROOT_CAUSE_PENALTY)
            if error_summary.get("primary_error_signature_id") is None
            else 0.0
        ),
        "low_parser_confidence": (
            float(LOW_PARSER_CONFIDENCE_PENALTY)
            if float(parser_confidence) < MIN_PARSER_CONFIDENCE
            else 0.0
        ),
    }


def compute_confidence_score(state: dict[str, Any]) -> int:
    """Score how much the upstream evidence supports a confident conclusion.

    Typed as ``dict[str, Any]`` rather than as ``LogSherlockState`` for the
    same reason every other node in this repository is: the state TypedDict
    lives in ``graph.py``, which imports this package, and naming it here would
    point the dependency arrow back at the application module.

    Args:
        state: The LogSherlock graph state. Reads ``parser_metrics`` and
            ``error_summary``, both read-only, both optional.

    Returns:
        An integer in ``[0, 100]``. :data:`BASE_SCORE` less every penalty in
        :func:`confidence_breakdown`, rounded once at the end and clamped.
        Rounding is deferred to the last step so two 0.4-point penalties cost a
        point together rather than nothing each.
    """
    penalties = confidence_breakdown(state)
    raw = BASE_SCORE - sum(penalties.values())
    score = max(MIN_SCORE, min(MAX_SCORE, round(raw)))

    logger.info(
        "Deterministic confidence: %d/100 (raw=%.2f penalties=%s)",
        score,
        raw,
        {name: round(points, 2) for name, points in penalties.items() if points},
    )
    return score


def apply_fallback_penalty(score: int) -> int:
    """Discount a score by :data:`FALLBACK_PENALTY`, clamped at :data:`MIN_SCORE`.

    Kept here rather than inline in the node so that every arithmetic step that
    can change the published score lives in one module, and so the clamp cannot
    be forgotten on the path least likely to be exercised in development.

    Args:
        score: A score already returned by :func:`compute_confidence_score`.

    Returns:
        The discounted score, never below :data:`MIN_SCORE`.
    """
    discounted = max(MIN_SCORE, score - FALLBACK_PENALTY)
    logger.info(
        "Applying the synthesis-fallback penalty: %d -> %d", score, discounted
    )
    return discounted

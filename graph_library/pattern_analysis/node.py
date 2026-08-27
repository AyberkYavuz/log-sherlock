"""The Pattern Analysis Node — behavioral reasoning over the deterministic pair.

This node runs *after* the Statistics and Timeline nodes rather than beside
them, and that placement is the whole point. Statistics answers "what does the
dataset contain?" and Timeline answers "how did it unfold?"; neither is allowed
to interpret its own output, and no node before this one reads both. The
patterns worth reporting — a failure spreading between components, activity
concentrating in one tenant, a baseline that shifted and never came back — are
visible only in the two together.

One batched call, like the error-analysis node and for the same reasons: the
findings are comparative (deciding that errors moved from the payment client to
the order service is impossible while looking at either distribution alone),
and the response schema carries summary-level fields that exist only for the
pair as a whole.

The node degrades rather than fails. A provider package that is not installed,
an absent credential, a call that raises, a model that answers in prose: all
produce a valid ``PatternSummary`` built by
:mod:`graph_library.pattern_analysis.fallback` from the deterministic inputs,
with the reason recorded in ``investigation_notes``. The arithmetic findings —
the peak, the onset, the concentrations — are exactly as accurate as they were
before the call failed; only the interpretation is missing. That resilience
costs visibility, so the whole pass is logged, with the degradation path at
``ERROR`` with a full traceback.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from graph_library.error_analysis.llm_factory import (
    DEFAULT_MODE,
    DEFAULT_PROVIDER,
    is_model_unavailable,
    iter_error_analysis_llms,
    normalize_mode,
    normalize_provider,
    structured_output_kwargs,
)
from graph_library.models import PatternAnalysisResult

from .fallback import build_fallback_summary
from .prompts import (
    SYSTEM_PROMPT,
    build_pattern_analysis_prompt,
    prompt_payload_sizes,
)

logger = logging.getLogger(__name__)

#: Emitted when both deterministic inputs are empty. Stated verbatim so a
#: reader of the report can tell "the system behaved normally" from "there was
#: nothing to look at".
NO_INPUT_NOTE = (
    "Pattern analysis: neither statistics nor a timeline were available, so no "
    "behavioral patterns could be derived."
)


def _invoke_with_model_fallback(
    provider: str,
    mode: str,
    prompt: str,
    *,
    schema: type[BaseModel] = PatternAnalysisResult,
) -> tuple[Any, str, str | None]:
    """Run the structured call, moving down the candidate chain on a dead model.

    The same shape as the error-analysis node's private helper of this name, and
    deliberately a separate copy rather than an import of it: that one is
    private to its module and its fallback note is worded for its own node. Both
    are thin wrappers over the *public* surface of
    :mod:`graph_library.error_analysis.llm_factory`, which is where the model
    routing, the candidate chain and the failure classification actually live.

    Only a model-identity failure is retried. An expired key, a rate limit or a
    timeout is raised straight through — swapping the model would burn another
    request and fail the same way.

    Args:
        provider: Which vendor to call.
        mode: Which tier to use.
        prompt: The rendered human turn.
        schema: The structured-output schema to bind.

    Returns:
        A ``(result, model_used, fallback_note)`` triple, never with a ``None``
        result. ``fallback_note`` is ``None`` on the happy path and otherwise a
        sentence for ``investigation_notes`` naming what was substituted.

    Raises:
        Exception: Whatever the *first* candidate raised, if every candidate
            fails. The first is the model the tier actually selected, so it is
            the failure worth reporting.
        RuntimeError: If a candidate answered without producing the structured
            response at all.
    """
    first_error: Exception | None = None
    requested: str | None = None
    structured_kwargs = structured_output_kwargs(provider)

    for model, llm in iter_error_analysis_llms(provider, mode):
        if requested is None:
            requested = model

        structured = llm.with_structured_output(schema, **structured_kwargs)
        logger.debug(
            "Invoking %s (model=%s schema=%s): system_prompt_chars=%d "
            "human_prompt_chars=%d structured_output=%s",
            type(llm).__name__,
            model,
            schema.__name__,
            len(SYSTEM_PROMPT),
            len(prompt),
            structured_kwargs.get("method", "<provider default>"),
        )
        try:
            result = structured.invoke([("system", SYSTEM_PROMPT), ("human", prompt)])
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            first_error = first_error or exc
            if not is_model_unavailable(exc):
                raise
            logger.warning(
                "Model %s is unavailable for provider=%s (%s: %s); trying the "
                "next candidate",
                model,
                provider,
                type(exc).__name__,
                exc,
            )
            continue

        if result is None:
            # Reachable only where the tool call is not forced — see
            # ``tool_choice`` in the factory's STRUCTURED_OUTPUT_OVERRIDES. The
            # parser yields ``None`` rather than raising when the model answers
            # in prose, which would otherwise surface as a bare validation
            # error against ``None``.
            raise RuntimeError(
                f"model {model!r} returned no structured response (the schema "
                "was offered as a tool call and the model did not use it)"
            )

        note = (
            None
            if model == requested
            else (
                f"Pattern analysis: model {requested!r} was unavailable, so "
                f"{model!r} answered instead. The {mode!r} tier for provider "
                f"{provider!r} needs updating."
            )
        )
        return result, model, note

    assert first_error is not None  # resolve_model_candidates is never empty
    raise first_error


def pattern_analysis_node(state: dict[str, Any]) -> dict[str, Any]:
    """Detect behavioral patterns across the statistics and the timeline.

    Args:
        state: The LogSherlock graph state. Reads ``statistics`` and
            ``timeline`` (the two deterministic reports this node reasons
            over), ``investigation_notes`` (what those passes could not
            measure), ``llm_provider`` and ``analysis_mode`` (which model to
            reason with, defaulting to ``"openai"`` / ``"standard"``) and
            ``application_name`` (prompt context). All are treated as
            read-only.

    Returns:
        A partial state delta containing:

            * ``pattern_summary`` — the
              :class:`~graph_library.models.PatternSummary` payload, present on
              every path including every failure path,
            * ``completed_stages`` — ``["pattern_analysis"]``,
            * ``investigation_notes`` — only when there is something to record:
              a degraded run, a substituted model, or an empty input.

        No other state field is touched. ``investigation_notes`` and
        ``completed_stages`` use additive reducers in the graph, so returning
        only this node's contributions is correct.
    """
    statistics = state.get("statistics") or {}
    timeline = state.get("timeline") or []
    # Read, never written back: these notes are the *input* the parser and the
    # timeline produced. Echoing them into the delta would duplicate every one
    # of them through the additive reducer.
    upstream_notes = state.get("investigation_notes") or []
    provider = normalize_provider(state.get("llm_provider") or DEFAULT_PROVIDER)
    mode = normalize_mode(state.get("analysis_mode") or DEFAULT_MODE)
    application_name = state.get("application_name")

    sizes = prompt_payload_sizes(statistics, timeline)
    logger.info(
        "Pattern analysis node starting: application=%s provider=%s mode=%s "
        "loggers=%d metadata_keys=%d milestones=%d buckets=%d/%d",
        application_name,
        provider,
        mode,
        sizes["loggers"],
        sizes["metadata_keys"],
        sizes["milestones"],
        sizes["buckets_sent"],
        sizes["buckets_total"],
    )

    if not statistics and not timeline:
        # Nothing to reason about, and nothing for the fallback to derive
        # either. Calling a model here would spend a request to be told the
        # input was empty.
        logger.info("No statistics and no timeline; skipping the LLM pass")
        return {
            "pattern_summary": build_fallback_summary(None, None).model_dump(),
            "investigation_notes": [NO_INPUT_NOTE],
            "completed_stages": ["pattern_analysis"],
        }

    try:
        prompt = build_pattern_analysis_prompt(
            statistics,
            timeline,
            upstream_notes,
            application_name=application_name,
        )
        result, model_used, fallback_note = _invoke_with_model_fallback(
            provider, mode, prompt
        )
    except Exception as exc:  # noqa: BLE001 - a derived summary beats a dead branch
        logger.error(
            "LLM reasoning failed for provider=%s mode=%s; returning the "
            "deterministic pattern summary",
            provider,
            mode,
            exc_info=True,
        )
        return {
            "pattern_summary": build_fallback_summary(
                statistics, timeline
            ).model_dump(),
            "investigation_notes": [
                f"Pattern analysis: LLM reasoning unavailable "
                f"({type(exc).__name__}: {exc}). Returning behavioral patterns "
                "derived arithmetically from the statistics and timeline."
            ],
            "completed_stages": ["pattern_analysis"],
        }

    notes: list[str] = []
    if fallback_note is not None:
        # A silent substitution would make the report unreproducible: the tier
        # the operator selected is not the model that answered.
        logger.warning("%s", fallback_note)
        notes.append(fallback_note)

    if not isinstance(result, PatternAnalysisResult):
        # ``with_structured_output`` yields a plain dict on some provider and
        # method combinations; normalize so the delta has one shape.
        logger.debug(
            "Normalizing %s response into PatternAnalysisResult", type(result).__name__
        )
        result = PatternAnalysisResult.model_validate(result)

    logger.info(
        "Pattern analysis node complete (model=%s): anomalies=%d "
        "correlations=%d metadata_insights=%d synthesis_chars=%d",
        model_used,
        len(result.anomalies),
        len(result.cross_logger_correlations),
        len(result.metadata_insights),
        len(result.behavioral_synthesis or ""),
    )

    delta: dict[str, Any] = {
        "pattern_summary": result.model_dump(),
        "completed_stages": ["pattern_analysis"],
    }
    if notes:
        delta["investigation_notes"] = notes
    return delta

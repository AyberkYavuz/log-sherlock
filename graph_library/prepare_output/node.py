"""The Prepare Output Node — the synthesis gate before persistence.

This is the graph's fan-in. Four analysis stages and the parser itself feed it,
and it is the first and only node that sees all of their output at once. Its job
has three parts, in this order:

    1. **Score the evidence deterministically.** Before anything is asked of a
       model, :mod:`graph_library.prepare_output.scoring` computes how much the
       upstream data supports a confident conclusion at all. That score is
       arithmetic over parser health and the error analysis — see that module
       for why it is not asked of the model.
    2. **Synthesize.** One batched structured-output call turns five payloads
       into a one-sentence ``root_cause`` and a multi-paragraph
       ``executive_summary``. One call rather than several because the
       conclusion is inherently comparative: deciding that a connection-pool
       error caused the order-service failures is impossible while looking at
       either finding alone.
    3. **Package.** Every upstream artifact is assembled verbatim into a
       ``structured_report``, partitioned by provenance, ready for the
       ``write_to_db`` node and for a UI to hydrate from. Nothing is summarized
       on the way in: a client that wants to draw the timeline needs the
       timeline.

The node degrades rather than fails, like every other LLM node here. A provider
package that is not installed, an absent credential, a call that raises or times
out, a model that answers in prose: all still publish a complete
``structured_report``, with fixed fallback text in place of the narrative, the
reason in ``investigation_notes``, and a further
:data:`~graph_library.prepare_output.scoring.FALLBACK_PENALTY` off the
confidence score. Every deterministic number in a degraded report is exactly as
accurate as it would have been in a healthy one; only the prose is missing, and
the discounted score is what says so.

The model's own ``llm_confidence_score`` never becomes the published
``confidence_score``. It is recorded, and a material disagreement with the
deterministic score is written to ``investigation_notes`` — see
:data:`CONFIDENCE_DIVERGENCE_THRESHOLD`.
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
from graph_library.models.prepare_output import (
    LLMPrepareOutputResult,
    StructuredInvestigationReport,
)

from .prompts import (
    SYSTEM_PROMPT,
    build_prepare_output_prompt,
    prompt_payload_sizes,
)
from .scoring import apply_fallback_penalty, compute_confidence_score

logger = logging.getLogger(__name__)

#: Published as ``root_cause`` when the synthesis pass could not run. Worded to
#: be unmistakably an absence rather than a finding: a downstream reader must
#: never be able to mistake the fallback for a diagnosis, and a UI showing this
#: string is showing the truth about the run.
FALLBACK_ROOT_CAUSE = (
    "Root cause undetermined — automated synthesis was unavailable for this "
    "investigation."
)

#: Published as ``executive_summary`` on the same path. It says what is missing
#: and where to look instead, because the report it sits in is complete: the
#: statistics, the timeline, the error signatures and the behavioral patterns
#: are all present and all exact.
FALLBACK_EXECUTIVE_SUMMARY = (
    "No narrative synthesis was produced for this investigation because the "
    "reasoning model could not be reached. The deterministic findings in this "
    "report are unaffected and complete: the statistics, the timeline, the "
    "error signatures and their counts, and the behavioral patterns are all "
    "present exactly as the analysis stages computed them. Review the error "
    "signatures and the timeline milestones directly, read the investigation "
    "notes for the reason this pass degraded, and note that the confidence "
    "score has been discounted to reflect the missing synthesis."
)

#: Emitted when every upstream artifact is empty. Stated verbatim so a reader
#: can tell "nothing went wrong" from "there was nothing to look at".
NO_INPUT_NOTE = (
    "Prepare output: no statistics, timeline, error signatures or behavioral "
    "patterns were available, so no synthesis could be produced."
)

#: How far the model's self-assessment may sit from the deterministic score
#: before the gap is written to ``investigation_notes``. The two measure
#: different things — signal clarity versus evidence completeness — so a gap is
#: expected and only a wide one is informative. A model that reports 95 on a
#: payload scored 55 has read a clean signal out of an incomplete dataset, and
#: that is worth a sentence in the investigation.
CONFIDENCE_DIVERGENCE_THRESHOLD = 25


def _invoke_with_model_fallback(
    provider: str,
    mode: str,
    prompt: str,
    *,
    schema: type[BaseModel] = LLMPrepareOutputResult,
) -> tuple[Any, str, str | None]:
    """Run the structured call, moving down the candidate chain on a dead model.

    The same shape as the error-analysis and pattern-analysis nodes' private
    helpers of this name, and deliberately a third copy rather than an import of
    either: both are private to their modules and both word their fallback note
    for their own node. All three are thin wrappers over the *public* surface of
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
                f"Prepare output: model {requested!r} was unavailable, so "
                f"{model!r} answered instead. The {mode!r} tier for provider "
                f"{provider!r} needs updating."
            )
        )
        return result, model, note

    assert first_error is not None  # resolve_model_candidates is never empty
    raise first_error


def _build_structured_report(
    state: dict[str, Any],
    *,
    root_cause: str,
    executive_summary: str,
    confidence_score: int,
    provider: str,
    mode: str,
) -> StructuredInvestigationReport:
    """Assemble the persisted report from the state and this node's synthesis.

    Shared by the healthy and the degraded paths, so a consumer never has to
    ask which one it received: both produce every key, and the difference shows
    up in the synthesis text, the confidence score and the investigation notes.

    Upstream artifacts are read defensively (``.get(...) or {}``) even though
    the fan-in guarantees every producer has run. The report is the thing that
    gets stored, and a ``KeyError`` here would lose an entire investigation that
    was otherwise complete — a missing section is recoverable, a dead node is
    not.

    Args:
        state: The LogSherlock graph state, read-only.
        root_cause: This node's one-sentence diagnosis, or the fallback text.
        executive_summary: This node's narrative, or the fallback text.
        confidence_score: The final published score, already discounted and
            clamped.
        provider: The normalized vendor this run used.
        mode: The normalized reasoning tier this run used.

    Returns:
        The complete report, JSON-serializable throughout.
    """
    return {
        "metadata": {
            "application_name": state.get("application_name") or "unknown",
            "investigation_timestamp": state.get("investigation_timestamp") or "",
            # The *normalized* values rather than the raw ones: this block is
            # the run's reproducibility record, and "anthropic" is what
            # answered when the caller typed "Claude".
            "analysis_mode": mode,
            "llm_provider": provider,
            "confidence_score": confidence_score,
            "parser_metrics": state.get("parser_metrics") or {},
        },
        "synthesis": {
            "root_cause": root_cause,
            "executive_summary": executive_summary,
            # The upstream notes as they stood when this report was built. This
            # node's own notes reach the graph's additive channel instead — see
            # the delta returned by :func:`prepare_output_node`.
            "investigation_notes": list(state.get("investigation_notes") or []),
        },
        "deterministic_outputs": {
            "statistics": state.get("statistics") or {},
            "timeline": state.get("timeline") or [],
        },
        "ai_insights": {
            "error_summary": state.get("error_summary") or {},
            "pattern_summary": state.get("pattern_summary") or {},
        },
    }


def _delta(
    state: dict[str, Any],
    *,
    root_cause: str,
    executive_summary: str,
    confidence_score: int,
    provider: str,
    mode: str,
    notes: list[str],
) -> dict[str, Any]:
    """Build the node's partial state delta. One shape, every path."""
    delta: dict[str, Any] = {
        "executive_summary": executive_summary,
        "root_cause": root_cause,
        "confidence_score": confidence_score,
        "structured_report": _build_structured_report(
            state,
            root_cause=root_cause,
            executive_summary=executive_summary,
            confidence_score=confidence_score,
            provider=provider,
            mode=mode,
        ),
        "completed_stages": ["prepare_output"],
    }
    if notes:
        delta["investigation_notes"] = notes
    return delta


def prepare_output_node(state: dict[str, Any]) -> dict[str, Any]:
    """Synthesize every upstream finding into the investigation's conclusion.

    Args:
        state: The LogSherlock graph state. Reads ``parser_metrics``,
            ``statistics``, ``timeline``, ``error_summary``,
            ``pattern_summary`` (the five upstream artifacts),
            ``investigation_notes`` (what those passes could not measure),
            ``historical_context`` (previous investigations, if any),
            ``application_name`` and ``investigation_timestamp`` (report
            identity), and ``llm_provider`` / ``analysis_mode`` (which model to
            reason with, defaulting to ``"openai"`` / ``"standard"``). All are
            treated as read-only.

    Returns:
        A partial state delta containing:

            * ``root_cause`` and ``executive_summary`` — the synthesis, present
              on every path including every failure path,
            * ``confidence_score`` — the deterministic score, discounted when
              the synthesis pass degraded,
            * ``structured_report`` — the complete
              :class:`~graph_library.models.StructuredInvestigationReport`,
            * ``completed_stages`` — ``["prepare_output"]``,
            * ``investigation_notes`` — only when there is something to record:
              a degraded run, a substituted model, an empty input, or a model
              whose self-assessment diverged sharply from the deterministic
              score.

        No other state field is touched. ``investigation_notes`` and
        ``completed_stages`` use additive reducers in the graph, so returning
        only this node's contributions is correct.
    """
    statistics = state.get("statistics") or {}
    timeline = state.get("timeline") or []
    error_summary = state.get("error_summary") or {}
    pattern_summary = state.get("pattern_summary") or {}
    parser_metrics = state.get("parser_metrics") or {}
    # Read, never written back: these notes are the *input* every upstream pass
    # produced. Echoing them into the delta would duplicate each one through
    # the additive reducer.
    upstream_notes = state.get("investigation_notes") or []
    historical_context = state.get("historical_context") or []
    provider = normalize_provider(state.get("llm_provider") or DEFAULT_PROVIDER)
    mode = normalize_mode(state.get("analysis_mode") or DEFAULT_MODE)

    confidence_score = compute_confidence_score(state)

    sizes = prompt_payload_sizes(
        statistics, timeline, error_summary, pattern_summary, historical_context
    )
    logger.info(
        "Prepare output node starting: application=%s provider=%s mode=%s "
        "confidence=%d signatures=%d/%d milestones=%d anomalies=%d "
        "history=%d/%d",
        state.get("application_name"),
        provider,
        mode,
        confidence_score,
        sizes["signatures_sent"],
        sizes["signatures_total"],
        sizes["milestones"],
        sizes["anomalies"],
        sizes["historical_sent"],
        sizes["historical_total"],
    )

    if not any((statistics, timeline, error_summary.get("signatures"), pattern_summary)):
        # Nothing to synthesize. Calling a model here would spend a request to
        # be told the input was empty, and the report still has to be published
        # so the run has an artifact — the same reasoning the pattern-analysis
        # node applies to its own empty-input case.
        logger.info("No upstream findings to synthesize; skipping the LLM pass")
        return _delta(
            state,
            root_cause=FALLBACK_ROOT_CAUSE,
            executive_summary=FALLBACK_EXECUTIVE_SUMMARY,
            confidence_score=apply_fallback_penalty(confidence_score),
            provider=provider,
            mode=mode,
            notes=[NO_INPUT_NOTE],
        )

    try:
        prompt = build_prepare_output_prompt(
            parser_metrics,
            statistics,
            timeline,
            error_summary,
            pattern_summary,
            upstream_notes,
            historical_context,
            application_name=state.get("application_name"),
            investigation_timestamp=state.get("investigation_timestamp"),
        )
        result, model_used, fallback_note = _invoke_with_model_fallback(
            provider, mode, prompt
        )
        if not isinstance(result, LLMPrepareOutputResult):
            # ``with_structured_output`` yields a plain dict on some provider
            # and method combinations; normalize so the delta has one shape.
            # Validated *inside* the guard rather than after it: a payload that
            # does not satisfy the schema is a provider failure like any other,
            # and letting a ``ValidationError`` escape here would turn the one
            # node that must always publish a report into the one that kills
            # the run.
            logger.debug(
                "Normalizing %s response into LLMPrepareOutputResult",
                type(result).__name__,
            )
            result = LLMPrepareOutputResult.model_validate(result)
    except Exception as exc:  # noqa: BLE001 - a complete report beats a dead branch
        logger.error(
            "LLM synthesis failed for provider=%s mode=%s; returning the "
            "deterministic report with fallback narrative text",
            provider,
            mode,
            exc_info=True,
        )
        return _delta(
            state,
            root_cause=FALLBACK_ROOT_CAUSE,
            executive_summary=FALLBACK_EXECUTIVE_SUMMARY,
            confidence_score=apply_fallback_penalty(confidence_score),
            provider=provider,
            mode=mode,
            notes=[
                f"Prepare output: LLM synthesis unavailable "
                f"({type(exc).__name__}: {exc}). The report carries the "
                "deterministic findings in full; the root cause and executive "
                "summary are placeholder text and the confidence score has "
                "been discounted accordingly."
            ],
        )

    notes: list[str] = []
    if fallback_note is not None:
        # A silent substitution would make the report unreproducible: the tier
        # the operator selected is not the model that answered.
        logger.warning("%s", fallback_note)
        notes.append(fallback_note)

    # The published score stays deterministic. The model's self-assessment is
    # about signal clarity and cannot see what never reached it, so it informs
    # the reader rather than the number.
    divergence = abs(result.llm_confidence_score - confidence_score)
    if divergence > CONFIDENCE_DIVERGENCE_THRESHOLD:
        notes.append(
            f"Prepare output: the model rated its own confidence at "
            f"{result.llm_confidence_score}/100 against a deterministic score "
            f"of {confidence_score}/100. The published score is the "
            "deterministic one; the gap reflects evidence the model could not "
            "see it was missing."
        )

    logger.info(
        "Prepare output node complete (model=%s): confidence=%d "
        "llm_confidence=%d root_cause_chars=%d summary_chars=%d",
        model_used,
        confidence_score,
        result.llm_confidence_score,
        len(result.root_cause or ""),
        len(result.executive_summary or ""),
    )

    return _delta(
        state,
        root_cause=result.root_cause,
        executive_summary=result.executive_summary,
        confidence_score=confidence_score,
        provider=provider,
        mode=mode,
        notes=notes,
    )

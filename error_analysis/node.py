"""The Error Analysis Node — the LLM half of the error investigation.

The deterministic half has already run by the time this module does any
reasoning: :mod:`error_analysis.fingerprint` has filtered the error records,
masked their variable tokens and collapsed them into a short ranked list of
signatures. This node adds only what arithmetic cannot supply — *which* of
those errors started the incident, and *why*.

The reasoning is a single batched call. Every signature goes into one prompt
and comes back in one structured response, rather than one call per signature,
for three reasons:

    * root cause is a *comparative* judgement — deciding that a payment-provider
      outage caused the booking failures is impossible while looking at either
      one alone;
    * one call per signature would multiply latency and cost by the signature
      count, in a node that already runs in parallel with three others;
    * the structured-output schema (:class:`~models.LLMErrorAnalysisResult`)
      naturally carries summary-level fields — the primary signature and the
      cascade narrative — that only exist for the batch as a whole.

The node degrades rather than fails. An empty payload, a provider package that
is not installed, or a call that raises all produce a valid ``ErrorSummary``
with the deterministic findings intact and the reason recorded in
``investigation_notes``; the graph's three sibling branches and the downstream
recommendation node keep running.

That resilience has a cost in visibility: a degraded run and a healthy one
return the same shape, and the only trace of the failure is one sentence in
``investigation_notes``. The module therefore logs the whole pass — entry
conditions, what the fingerprinting produced, the size of the prompt, and the
verdicts that came back — with the degradation path logged at ``ERROR`` with a
full traceback, since that note is a summary and not a diagnosis.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from models import (
    ErrorSignature,
    ErrorSummary,
    LLMErrorAnalysisResult,
)

from .fingerprint import build_error_summary
from .llm_factory import (
    DEFAULT_MODE,
    DEFAULT_PROVIDER,
    is_model_unavailable,
    iter_error_analysis_llms,
    normalize_mode,
    normalize_provider,
    structured_output_kwargs,
)

logger = logging.getLogger(__name__)

#: The node's standing instructions. Written to fight the two failure modes a
#: model reliably shows on log data: calling the *loudest* error the root cause
#: (it is usually the downstream symptom, logged once per affected request), and
#: inventing detail that is not in the payload.
SYSTEM_PROMPT = """\
You are a senior site-reliability engineer performing root-cause analysis on \
application logs.

You will receive error signatures. Each signature is a GROUP of identical \
errors that were collapsed by a deterministic fingerprinting pass: `template` \
is the message with variable tokens replaced by placeholders (<IP>, <PORT>, \
<UUID>, <NUM>, <HEX>, <ADDR>), and `count` is how many times that error \
actually occurred.

Your task:
1. Decide which signatures are PRIMARY (a genuine trigger: a dependency \
failing, a resource exhausted, a misconfiguration, an unhandled defect) and \
which are SECONDARY (downstream consequences: request failures, workflow \
aborts, retries and timeouts caused by a primary error).
2. Choose exactly one `primary_error_signature_id` for the most likely root \
cause, or null if the evidence genuinely does not support a single choice.
3. Explain how the primary error cascaded into the secondary ones.

Rules:
- A high `count` indicates blast radius, NOT causation. A single \
infrastructure error occurring 3 times routinely causes 900 downstream request \
failures; the root cause is usually the low-count error that appears FIRST.
- Use `first_seen` ordering as causal evidence: an error cannot be caused by \
one that started after it.
- Ground every claim in the signatures provided. Do not invent errors, \
services, timestamps or counts that are not present.
- Return an evaluation for EVERY signature id you were given, and use those \
ids exactly as written.
- Keep each explanation to 1-2 sentences.\
"""

#: Fields sent to the model, per signature. ``is_root_cause_candidate`` and
#: ``explanation`` are excluded on purpose — they are the model's output, and
#: showing it their empty defaults would only invite it to echo them back.
_PROMPT_FIELDS: tuple[str, ...] = (
    "signature_id",
    "template",
    "severity",
    "count",
    "first_seen",
    "last_seen",
    "loggers",
    "sample_messages",
)


def build_analysis_prompt(
    signatures: list[ErrorSignature],
    *,
    application_name: str | None = None,
) -> str:
    """Render the batch of signatures as the human turn of the prompt.

    The signatures are serialized as JSON rather than prose: it is the format
    the model is most reliably able to align with the ``signature_id`` values it
    must echo back, and it keeps the mapping between a template and its counts
    unambiguous.

    Args:
        signatures: The deterministic signatures, already ranked by volume.
        application_name: The application under investigation, included as
            context when the caller supplied one.

    Returns:
        The rendered prompt string.
    """
    payload = [
        {field: signature.get(field) for field in _PROMPT_FIELDS}
        for signature in signatures
    ]

    header = (
        f"Application under investigation: {application_name}\n\n"
        if application_name
        else ""
    )

    return (
        f"{header}"
        f"Analyze the following {len(signatures)} error "
        f"{'signature' if len(signatures) == 1 else 'signatures'}, ordered by "
        "descending occurrence count.\n\n"
        f"{json.dumps(payload, indent=2, default=str)}\n\n"
        "Identify the root cause and evaluate every signature."
    )


def _merge_evaluations(
    signatures: list[ErrorSignature], result: LLMErrorAnalysisResult
) -> tuple[list[ErrorSignature], list[str]]:
    """Copy the model's verdicts onto the deterministic signatures.

    The deterministic fields are authoritative and are never overwritten — the
    model contributes exactly two fields per signature. A verdict naming an
    unknown ``signature_id`` (a hallucinated or malformed id) is discarded and
    reported rather than being force-fitted onto a signature it may not
    describe.

    Args:
        signatures: The deterministic signatures.
        result: The model's structured response.

    Returns:
        A ``(signatures, notes)`` pair. ``signatures`` is a new list of new
        dicts — the input is not mutated. ``notes`` records unmatched or
        missing evaluations.
    """
    by_id = {
        evaluation.signature_id: evaluation for evaluation in result.evaluations
    }
    known = {signature["signature_id"] for signature in signatures}

    merged: list[ErrorSignature] = []
    for signature in signatures:
        evaluation = by_id.get(signature["signature_id"])
        merged.append(
            {
                **signature,
                "is_root_cause_candidate": (
                    bool(evaluation.is_root_cause_candidate) if evaluation else False
                ),
                "explanation": evaluation.explanation if evaluation else "",
            }
        )

    notes: list[str] = []

    unknown = sorted(set(by_id) - known)
    if unknown:
        notes.append(
            "Error analysis: the model returned "
            f"{len(unknown)} evaluation(s) for unknown signature "
            f"{'id' if len(unknown) == 1 else 'ids'} ({', '.join(unknown)}); "
            "they were discarded."
        )

    unevaluated = [sid for sid in known if sid not in by_id]
    if unevaluated:
        notes.append(
            f"Error analysis: the model did not evaluate "
            f"{len(unevaluated)} of {len(known)} signatures; they retain their "
            "deterministic defaults."
        )

    return merged, notes


def _resolve_primary(
    signatures: list[ErrorSignature], result: LLMErrorAnalysisResult
) -> tuple[str | None, list[str]]:
    """Validate the model's root-cause pick against the real signature ids."""
    primary = result.primary_error_signature_id
    if primary is None:
        return None, []

    known = {signature["signature_id"] for signature in signatures}
    if primary in known:
        return primary, []

    return None, [
        "Error analysis: the model named an unknown signature id "
        f"({primary!r}) as the primary error; it was dropped."
    ]


def _invoke_with_model_fallback(
    provider: str, mode: str, prompt: str
) -> tuple[Any, str, str | None]:
    """Run the batched call, moving down the candidate chain on a dead model.

    Only a model-identity failure is retried. An expired key, a rate limit or a
    timeout is raised straight through — swapping the model would burn another
    request and fail the same way.

    Args:
        provider: Which vendor to call.
        mode: Which tier to use.
        prompt: The rendered human turn.

    Returns:
        A ``(result, model_used, fallback_note)`` triple, never with a ``None``
        result. ``fallback_note`` is ``None`` on the happy path and otherwise a
        sentence for ``investigation_notes`` naming what was substituted for
        what.

    Raises:
        Exception: Whatever the *first* candidate raised, if every candidate
            fails. The first is the one the tier actually selected, so it is
            the failure worth reporting; the later ones are consequences of
            this function's own retrying.
        RuntimeError: If a candidate answered without producing the structured
            response at all.
    """
    first_error: Exception | None = None
    requested: str | None = None
    # How the schema is requested is a property of the endpoint, not of the
    # model, so it is resolved once outside the fallback loop.
    structured_kwargs = structured_output_kwargs(provider)

    for model, llm in iter_error_analysis_llms(provider, mode):
        if requested is None:
            requested = model

        structured = llm.with_structured_output(
            LLMErrorAnalysisResult, **structured_kwargs
        )
        logger.debug(
            "Invoking %s (model=%s): system_prompt_chars=%d human_prompt_chars=%d "
            "structured_output=%s",
            type(llm).__name__,
            model,
            len(SYSTEM_PROMPT),
            len(prompt),
            structured_kwargs.get("method", "<provider default>"),
        )
        try:
            result = structured.invoke(
                [("system", SYSTEM_PROMPT), ("human", prompt)]
            )
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
            # Only reachable where the tool call is not forced — see
            # ``tool_choice`` in
            # :data:`~error_analysis.llm_factory.STRUCTURED_OUTPUT_OVERRIDES`.
            # The parser yields ``None`` rather than raising when the model
            # answers in prose, which would otherwise surface downstream as a
            # bare ValidationError against ``None``.
            raise RuntimeError(
                f"model {model!r} returned no structured response (the schema "
                "was offered as a tool call and the model did not use it)"
            )

        note = (
            None
            if model == requested
            else (
                f"Error analysis: model {requested!r} was unavailable, so "
                f"{model!r} answered instead. The {mode!r} tier for provider "
                f"{provider!r} needs updating."
            )
        )
        return result, model, note

    assert first_error is not None  # resolve_model_candidates is never empty
    raise first_error


def error_analysis_node(state: dict[str, Any]) -> dict[str, Any]:
    """Identify, group and explain the errors in ``parsed_logs``.

    Args:
        state: The LogSherlock graph state. Reads ``parsed_logs`` (the records
            to analyze), ``llm_provider`` and ``analysis_mode`` (which model to
            reason with, defaulting to ``"openai"`` / ``"standard"``) and
            ``application_name`` (prompt context). All are treated as read-only.

    Returns:
        A partial state delta containing exactly:

            * ``error_summary`` — the :class:`~models.ErrorSummary`,
            * ``investigation_notes`` — how the errors were grouped, what was
              omitted, and any degradation that occurred,
            * ``completed_stages`` — ``["error_analysis"]``.

        No other state field is touched. ``investigation_notes`` and
        ``completed_stages`` use additive reducers in the graph, so returning
        only this node's contributions is correct even though three sibling
        nodes run in the same superstep.
    """
    parsed_logs = state.get("parsed_logs") or []
    # Normalized here rather than at the call site so the logs below name the
    # provider that will actually be dialled, not the string the user typed.
    provider = normalize_provider(state.get("llm_provider") or DEFAULT_PROVIDER)
    mode = normalize_mode(state.get("analysis_mode") or DEFAULT_MODE)
    application_name = state.get("application_name")

    logger.info(
        "Error analysis node starting: application=%s provider=%s mode=%s "
        "parsed_log_entries=%d",
        application_name,
        provider,
        mode,
        len(parsed_logs),
    )

    summary, notes = build_error_summary(parsed_logs)
    logger.info(
        "Fingerprinting produced %d signature(s) from %d error-level entries",
        len(summary["signatures"]),
        summary["total_errors_analyzed"],
    )

    if not summary["signatures"]:
        # ``build_error_summary`` has already explained why in its note; adding
        # a second sentence here would only restate it.
        logger.info("No signatures to reason about; skipping the LLM pass")
        return {
            "error_summary": summary,
            "investigation_notes": notes,
            "completed_stages": ["error_analysis"],
        }

    try:
        prompt = build_analysis_prompt(
            summary["signatures"], application_name=application_name
        )
        result, model_used, fallback_note = _invoke_with_model_fallback(
            provider, mode, prompt
        )
    except Exception as exc:  # noqa: BLE001 - a degraded summary beats a dead branch
        # The deterministic findings are still worth publishing: counts,
        # templates and timings are exactly as accurate as they were before the
        # call failed. Only the interpretation is missing, and the note says so.
        logger.error(
            "LLM reasoning failed for provider=%s mode=%s; returning "
            "deterministic signatures only",
            provider,
            mode,
            exc_info=True,
        )
        notes.append(
            f"Error analysis: LLM reasoning unavailable "
            f"({type(exc).__name__}: {exc}). Returning deterministic error "
            "signatures without root-cause evaluation."
        )
        return {
            "error_summary": summary,
            "investigation_notes": notes,
            "completed_stages": ["error_analysis"],
        }

    if fallback_note is not None:
        # A silent substitution would make the report unreproducible: the tier
        # the operator selected is not the model that answered.
        logger.warning("%s", fallback_note)
        notes.append(fallback_note)

    if not isinstance(result, LLMErrorAnalysisResult):
        # ``with_structured_output`` can yield a plain dict depending on the
        # provider and method; normalize so the merge below has one shape.
        logger.debug(
            "Normalizing %s response into LLMErrorAnalysisResult", type(result).__name__
        )
        result = LLMErrorAnalysisResult.model_validate(result)

    logger.info(
        "LLM returned %d evaluation(s); primary_error_signature_id=%s",
        len(result.evaluations),
        result.primary_error_signature_id,
    )

    signatures, merge_notes = _merge_evaluations(summary["signatures"], result)
    primary, primary_notes = _resolve_primary(signatures, result)

    for note in (*merge_notes, *primary_notes):
        logger.warning("%s", note)

    final_summary: ErrorSummary = {
        **summary,
        "signatures": signatures,
        "primary_error_signature_id": primary,
        "cascading_impact_summary": result.cascading_impact_summary,
    }

    logger.info(
        "Error analysis node complete: signatures=%d primary=%s "
        "cascading_impact_summary_chars=%d",
        len(signatures),
        primary,
        len(result.cascading_impact_summary or ""),
    )

    return {
        "error_summary": final_summary,
        "investigation_notes": [*notes, *merge_notes, *primary_notes],
        "completed_stages": ["error_analysis"],
    }

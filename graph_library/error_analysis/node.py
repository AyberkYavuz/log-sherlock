"""The Error Analysis Node — the LLM half of the error investigation.

The deterministic half has already run by the time this module does any
reasoning: :mod:`graph_library.error_analysis.fingerprint` has filtered the error records,
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
    * the structured-output schema (:class:`~graph_library.models.LLMErrorAnalysisResult`)
      naturally carries summary-level fields — the primary signature and the
      cascade narrative — that only exist for the batch as a whole.

One call, except when the caller opts into web search. ``enable_web_search``
turns the node into two passes with a detour between them, because the model
knows the common failures cold and the rare ones not at all:

    * **pass 1** shows it the signature templates and asks a single question —
      is anything here obscure enough to look up? The expected answer, for the
      connection refusals and null dereferences that make up most payloads, is
      *no*, and that answer costs one cheap call and nothing else. When the
      answer is *yes* the node returns the queries and stops, and the router in
      :mod:`graph` sends state through :mod:`graph_library.web_search.node`;
    * **pass 2** is the call described above, with whatever documentation came
      back folded into the prompt.

The two passes are the same function, distinguished only by whether
``search_context`` is ``None`` (undecided) or a list (decided, possibly empty).
With the flag off the node never enters pass 1 and behaves exactly as it did
before the capability existed — no extra call, no extra latency, same output.

The node degrades rather than fails. An empty payload, a provider package that
is not installed, a call that raises, a search that finds nothing: all produce
a valid ``ErrorSummary`` with the deterministic findings intact and the reason
recorded in ``investigation_notes``; the graph's three sibling branches and the
downstream prepare_output node keep running.

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
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from graph_library.models import (
    MAX_SEARCH_QUERIES,
    ErrorSignature,
    ErrorSummary,
    LLMErrorAnalysisResult,
    LLMSearchDecision,
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

#: The response schema of one structured call. Bound to the concrete class the
#: caller passes so both passes keep their own return type — the analysis pass
#: gets an :class:`~graph_library.models.LLMErrorAnalysisResult` back, the
#: decision pass an :class:`~graph_library.models.LLMSearchDecision`.
SchemaT = TypeVar("SchemaT", bound=BaseModel)

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

#: The decision pass's instructions. Written to make *no search* the easy
#: answer: the flag is opt-in, but once it is on this prompt runs against every
#: payload, and a model that reaches for a search whenever one is offered would
#: add a network round trip to investigations that never needed one.
SEARCH_DECISION_SYSTEM_PROMPT = f"""\
You are a senior site-reliability engineer triaging error signatures before a \
root-cause analysis.

Your only job is to decide whether any of these errors is obscure enough that \
you would have to look up external documentation to explain it. You are not \
analyzing the errors yet.

Return an EMPTY `queries` list — the normal answer — when the errors are the \
everyday kind a senior engineer already understands without help: connection \
refused, timeouts, out of memory, null dereferences, HTTP 4xx/5xx, permission \
denied, disk full, failed migrations, and the standard exceptions of mainstream \
languages and frameworks.

Return 1 to {MAX_SEARCH_QUERIES} queries ONLY when a signature is genuinely \
unfamiliar:
- a vendor- or framework-specific error code with no self-evident meaning;
- an error from a niche library, protocol, driver or runtime internal;
- wording that suggests a known bug or a version-specific quirk.

Rules:
- Write queries ONLY for the signatures that are genuinely unfamiliar. Deciding \
that one signature needs a lookup does not mean the others do: a payload with \
one obscure error and four ordinary ones needs ONE query, not five. Signatures \
you already understand get no query even when they appear alongside one you do \
not.
- Write each query as a self-contained web search someone would actually type: \
the distinctive error text plus its technology. "Vitess ERR_VT09027 vtgate \
transaction" is useful; "what does this error mean" is not.
- Search the specific error, never its generic category.
- Never return more than {MAX_SEARCH_QUERIES} queries.
- When in doubt, return none. A search costs latency on the critical path, and \
documentation for the wrong error is worse than no documentation.\
"""

#: Fields sent to the decision pass, per signature. Deliberately fewer than
#: :data:`_PROMPT_FIELDS`: the question is "have I heard of this?", which the
#: wording answers. Counts and timings are evidence for *causation* and belong
#: to pass 2; sending them here would only make a cheap call more expensive.
_DECISION_FIELDS: tuple[str, ...] = (
    "signature_id",
    "template",
    "severity",
    "loggers",
)

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


def build_search_decision_prompt(
    signatures: list[ErrorSignature],
    *,
    application_name: str | None = None,
) -> str:
    """Render the human turn of the pass-1 "is this obscure?" call.

    Args:
        signatures: The deterministic signatures, already ranked by volume.
        application_name: The application under investigation. Worth more here
            than in pass 2 — the stack a message came from is often what makes
            an unfamiliar code searchable.

    Returns:
        The rendered prompt string.
    """
    payload = [
        {field: signature.get(field) for field in _DECISION_FIELDS}
        for signature in signatures
    ]

    header = (
        f"Application under investigation: {application_name}\n\n"
        if application_name
        else ""
    )

    return (
        f"{header}"
        f"Here are {len(signatures)} error "
        f"{'signature' if len(signatures) == 1 else 'signatures'} from one "
        "application.\n\n"
        f"{json.dumps(payload, indent=2, default=str)}\n\n"
        "Decide whether any of them requires external documentation, and "
        "return the queries you would run."
    )


def _render_search_context(snippets: list[str]) -> str:
    """Render retrieved documentation as the reference section of the prompt.

    Framed as *reference material* rather than as evidence, and fenced off from
    the signatures, because the two have very different standing. The
    signatures are what happened; the snippets are pages a search engine
    thought were related, and the model has to be told it may discard them. A
    node that fetches documentation and then implies the model must use it has
    only moved the hallucination one step upstream.
    """
    numbered = "\n\n".join(
        f"[{index}] {snippet}" for index, snippet in enumerate(snippets, start=1)
    )

    return (
        "\n\nEXTERNAL REFERENCE MATERIAL\n"
        f"The following {len(snippets)} "
        f"{'excerpt was' if len(snippets) == 1 else 'excerpts were'} retrieved "
        "from a web search run against the less familiar signatures above. "
        "Treat them as background reading, not as evidence:\n\n"
        f"{numbered}\n\n"
        "Use an excerpt only where it plainly describes one of the signatures, "
        "and ignore the rest. The signatures remain the only record of what "
        "actually happened."
    )


def build_analysis_prompt(
    signatures: list[ErrorSignature],
    *,
    application_name: str | None = None,
    search_context: list[str] | None = None,
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
        search_context: Documentation snippets retrieved by
            :mod:`graph_library.web_search.node`. ``None`` and ``[]`` render identically —
            nothing is added — because "search was never enabled" and "search
            found nothing" are the same fact from the prompt's point of view.

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

    reference = _render_search_context(search_context) if search_context else ""

    return (
        f"{header}"
        f"Analyze the following {len(signatures)} error "
        f"{'signature' if len(signatures) == 1 else 'signatures'}, ordered by "
        "descending occurrence count.\n\n"
        f"{json.dumps(payload, indent=2, default=str)}"
        f"{reference}\n\n"
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
    provider: str,
    mode: str,
    prompt: str,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    schema: type[SchemaT] = LLMErrorAnalysisResult,
) -> tuple[SchemaT, str, str | None]:
    """Run a structured call, moving down the candidate chain on a bad answer.

    Two classes of failure are retried against the next candidate, because both
    are properties of the *model* rather than of the account calling it:

        * a model-identity failure — the id is retired and answers ``404``;
        * an unusable answer — no tool call at all, or a tool call whose
          arguments do not validate against ``schema``. Anthropic's current
          generation has been seen wrapping those arguments in a ``content``
          key; :class:`~graph_library.models.LLMErrorAnalysisResult` unwraps
          that shape itself, and anything it cannot repair lands here.

    An expired key, a rate limit or a timeout is raised straight through —
    swapping the model would burn another request and fail the same way.

    Both passes route through here rather than only the main one: a retired
    model id breaks the cheap decision call exactly as it breaks the expensive
    analysis call, and the search decision is the pass that runs *first*, so it
    is the one that meets the dead model.

    Args:
        provider: Which vendor to call.
        mode: Which tier to use.
        prompt: The rendered human turn.
        system_prompt: The standing instructions. Defaults to the analysis
            pass's, since that is the call this node exists for.
        schema: The structured-output schema to bind. Defaults, likewise, to
            the analysis pass's.

    Returns:
        A ``(result, model_used, fallback_note)`` triple. ``result`` is always a
        validated instance of ``schema`` — never ``None``, and never the plain
        dict some provider/method combinations yield — so callers can use it
        without re-checking its shape. ``fallback_note`` is ``None`` on the
        happy path and otherwise a sentence for ``investigation_notes`` naming
        what was substituted for what.

    Raises:
        Exception: Whatever the *first* candidate failed with, if every
            candidate fails. The first is the one the tier actually selected,
            so it is the failure worth reporting; the later ones are
            consequences of this function's own retrying. Callers are expected
            to catch this and degrade — it is the node's only failure signal,
            since nothing escapes the loop uncaught.
    """
    first_error: Exception | None = None
    requested: str | None = None
    # How the schema is requested is a property of the endpoint, not of the
    # model, so it is resolved once outside the fallback loop.
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
            len(system_prompt),
            len(prompt),
            structured_kwargs.get("method", "<provider default>"),
        )
        try:
            result = structured.invoke(
                [("system", system_prompt), ("human", prompt)]
            )
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            first_error = first_error or exc
            if is_model_unavailable(exc):
                logger.warning(
                    "Model %s is unavailable for provider=%s (%s: %s); trying "
                    "the next candidate",
                    model,
                    provider,
                    type(exc).__name__,
                    exc,
                )
                continue
            if isinstance(exc, ValueError):
                # Every way the provider's parser reports a malformed answer is
                # a ValueError: Pydantic's ``ValidationError`` when the tool
                # arguments do not fit the schema, LangChain's
                # ``OutputParserException`` when the response is not parseable
                # at all, and a bare ``ValueError`` when the arguments are not
                # even a dict. Matched by that shared base rather than by class
                # so this does not need a runtime langchain import.
                logger.warning(
                    "Model %s answered provider=%s with a response that does "
                    "not fit %s (%s: %s); trying the next candidate",
                    model,
                    provider,
                    schema.__name__,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                continue
            raise

        if result is None:
            # Only reachable where the tool call is not forced — see
            # ``tool_choice`` in
            # :data:`~graph_library.error_analysis.llm_factory.STRUCTURED_OUTPUT_OVERRIDES`.
            # The parser yields ``None`` rather than raising when the model
            # answers in prose, which would otherwise surface downstream as a
            # bare ValidationError against ``None``.
            missing = RuntimeError(
                f"model {model!r} returned no structured response (the schema "
                "was offered as a tool call and the model did not use it)"
            )
            first_error = first_error or missing
            logger.warning("%s; trying the next candidate", missing)
            continue

        try:
            # ``with_structured_output`` yields a plain dict on some
            # provider/method combinations, and the schema's own unwrapping
            # runs here too — which is why this sits inside the loop rather
            # than at the call site, where a ValidationError would escape past
            # the fallback chain and take the node down with it.
            result = (
                result
                if isinstance(result, schema)
                else schema.model_validate(result)
            )
        except ValidationError as exc:
            first_error = first_error or exc
            logger.warning(
                "Model %s answered provider=%s with a %s that does not "
                "validate as %s (%s); trying the next candidate",
                model,
                provider,
                type(result).__name__,
                schema.__name__,
                exc,
                exc_info=True,
            )
            continue

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


def decide_search_queries(
    provider: str,
    mode: str,
    signatures: list[ErrorSignature],
    *,
    application_name: str | None = None,
) -> list[str]:
    """Ask the model which signatures, if any, need looking up.

    Never raises. A failed decision is treated as "no search needed" rather
    than as a failure, because the alternative is letting an optional
    enhancement break an investigation that would otherwise have succeeded —
    the same trade the node makes everywhere else, and an easier one here,
    since the fallback is simply the behaviour of the flag being off.

    Args:
        provider: Which vendor to call.
        mode: Which tier to use.
        signatures: The deterministic signatures to triage.
        application_name: The application under investigation.

    Returns:
        Between zero and :data:`~graph_library.models.MAX_SEARCH_QUERIES` queries. Empty
        means no search: either the model wanted none, or asking failed.
    """
    prompt = build_search_decision_prompt(
        signatures, application_name=application_name
    )

    try:
        decision, model_used, _ = _invoke_with_model_fallback(
            provider,
            mode,
            prompt,
            system_prompt=SEARCH_DECISION_SYSTEM_PROMPT,
            schema=LLMSearchDecision,
        )
    except Exception:  # noqa: BLE001 - an unsearched run beats a dead branch
        logger.warning(
            "Search decision failed for provider=%s mode=%s; continuing without "
            "web search",
            provider,
            mode,
            exc_info=True,
        )
        return []

    # No shape check here: ``_invoke_with_model_fallback`` returns a validated
    # ``LLMSearchDecision`` or raises, and this call site is *outside* the
    # node's own try block — a ValidationError escaping to here would crash the
    # graph thread rather than degrade it.
    queries = [query.strip() for query in decision.queries if query and query.strip()]

    if len(queries) > MAX_SEARCH_QUERIES:
        # The cap is in the schema description, which is a request rather than
        # a constraint; enforcing it here is what makes it a limit.
        logger.warning(
            "Model %s asked for %d queries; keeping the first %d",
            model_used,
            len(queries),
            MAX_SEARCH_QUERIES,
        )
        queries = queries[:MAX_SEARCH_QUERIES]

    logger.info(
        "Search decision (model=%s): %d query/queries — %s",
        model_used,
        len(queries),
        decision.reasoning or "<no reasoning given>",
    )
    return queries


def error_analysis_node(state: dict[str, Any]) -> dict[str, Any]:
    """Identify, group and explain the errors in ``parsed_logs``.

    Runs once with web search off, and up to twice with it on — see the module
    docstring. Which pass this call is running is read from state, not from an
    argument, so the node stays a plain ``state -> delta`` function that the
    graph can route back into.

    Args:
        state: The LogSherlock graph state. Reads ``parsed_logs`` (the records
            to analyze), ``llm_provider`` and ``analysis_mode`` (which model to
            reason with, defaulting to ``"openai"`` / ``"standard"``),
            ``application_name`` (prompt context), ``enable_web_search`` (opt
            in to the two-pass path, default off) and ``search_context`` (what
            :mod:`graph_library.web_search.node` retrieved, and the marker for which pass
            this is). All are treated as read-only.

    Returns:
        On the **decision pass** — reached only when web search is enabled and
        the model asked for queries — a delta containing exactly
        ``search_queries``. No summary, no ``completed_stages``: the node has
        not finished, and the router sends state to ``web_search`` and back
        here.

        On the **analysis pass**, which is every other call, a delta containing:

            * ``error_summary`` — the :class:`~graph_library.models.ErrorSummary`,
            * ``investigation_notes`` — how the errors were grouped, what was
              omitted, and any degradation that occurred,
            * ``completed_stages`` — ``["error_analysis"]``,
            * ``search_context`` — only when this call decided no search was
              needed, recording that decision so the router does not loop.

        No other state field is touched. ``investigation_notes`` and
        ``completed_stages`` use additive reducers in the graph, so returning
        only this node's contributions is correct even though three sibling
        nodes run in the same superstep. The fingerprinting notes are attached
        on the analysis pass alone — emitting them on both would duplicate
        every one of them through those same additive reducers.
    """
    parsed_logs = state.get("parsed_logs") or []
    # Normalized here rather than at the call site so the logs below name the
    # provider that will actually be dialled, not the string the user typed.
    provider = normalize_provider(state.get("llm_provider") or DEFAULT_PROVIDER)
    mode = normalize_mode(state.get("analysis_mode") or DEFAULT_MODE)
    application_name = state.get("application_name")
    enable_web_search = bool(state.get("enable_web_search"))
    # ``None`` means undecided, which is what makes this the decision pass. An
    # empty list is a decision — "nothing here is worth looking up", or "the
    # search came back with nothing" — and both mean: get on with the analysis.
    search_context = state.get("search_context")

    logger.info(
        "Error analysis node starting: application=%s provider=%s mode=%s "
        "parsed_log_entries=%d web_search=%s search_context=%s",
        application_name,
        provider,
        mode,
        len(parsed_logs),
        "enabled" if enable_web_search else "disabled",
        "undecided" if search_context is None else f"{len(search_context)} snippet(s)",
    )

    summary, notes = build_error_summary(parsed_logs)
    logger.info(
        "Fingerprinting produced %d signature(s) from %d error-level entries",
        len(summary["signatures"]),
        summary["total_errors_analyzed"],
    )

    if not summary["signatures"]:
        # ``build_error_summary`` has already explained why in its note; adding
        # a second sentence here would only restate it. Note that this returns
        # before the decision pass: with nothing to explain there is nothing to
        # look up, and a search here would cost a round trip to enrich an empty
        # report.
        logger.info("No signatures to reason about; skipping the LLM pass")
        return {
            "error_summary": summary,
            "investigation_notes": notes,
            "completed_stages": ["error_analysis"],
        }

    # -- pass 1: is anything here worth looking up? --------------------------
    decided_no_search = False
    if enable_web_search and search_context is None:
        queries = decide_search_queries(
            provider, mode, summary["signatures"], application_name=application_name
        )
        if queries:
            logger.info(
                "Handing %d query/queries to the web search node: %s",
                len(queries),
                ", ".join(repr(query) for query in queries),
            )
            # Deliberately the whole delta: no summary and no completed stage,
            # because this node has not finished. Withholding the fingerprint
            # notes here is what keeps them from being appended twice.
            return {"search_queries": queries}

        logger.info("Nothing obscure enough to search for; proceeding directly")
        search_context = []
        decided_no_search = True

    # -- pass 2: the analysis ------------------------------------------------
    # Recorded only when *this* call made the decision. Writing it back is what
    # stops the router sending an unsearched run round the loop a second time;
    # when the web search node supplied the context, re-publishing it would just
    # be echoing state back at the graph.
    search_delta: dict[str, Any] = {"search_context": []} if decided_no_search else {}

    try:
        prompt = build_analysis_prompt(
            summary["signatures"],
            application_name=application_name,
            search_context=search_context,
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
            **search_delta,
        }

    if fallback_note is not None:
        # A silent substitution would make the report unreproducible: the tier
        # the operator selected is not the model that answered.
        logger.warning("%s", fallback_note)
        notes.append(fallback_note)

    # ``result`` is a validated LLMErrorAnalysisResult: the dict-shaped and
    # envelope-wrapped responses are normalized inside the fallback loop, where
    # a failure to do so is a fallback rather than an exception. Validating it
    # here instead would put it outside the try above, which is how a wrapped
    # Anthropic payload took the whole node down.
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
        **search_delta,
    }

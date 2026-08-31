"""The error-analysis models produced by the Error Analysis Node.

``ErrorSummary`` answers one question — *"which errors happened, how often, and
which of them actually started the incident?"* — and it answers it in two
distinct passes that this module keeps deliberately separate:

    * a **deterministic** pass (see :mod:`graph_library.error_analysis.fingerprint`) that
      filters error-severity records, masks their variable tokens into a stable
      ``template`` and collapses identical templates into counted signatures.
      Given the same ``parsed_logs`` it always produces the same signatures, in
      the same order, with the same ids;
    * an **LLM** pass that reads those signatures and fills in the two fields
      the arithmetic cannot supply — ``is_root_cause_candidate`` and
      ``explanation`` — plus the summary-level ``primary_error_signature_id``
      and ``cascading_impact_summary``.

The split is visible in the types. ``ErrorSignature`` / ``ErrorSummary`` are
:class:`~typing.TypedDict` s like every other shared model, so an error summary
*is* the plain dict that flows through LangGraph state — no serialization
layer. The ``LLM*`` classes are :class:`~pydantic.BaseModel` s instead, because
they exist for exactly one purpose: to be handed to
``ChatModel.with_structured_output()`` as the response schema. They are a
transport contract with the model, never graph state; the node merges their
contents into the TypedDicts and discards them.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, Field

from .base import _ToolArgumentEnvelope

#: The chat providers the Error Analysis Node can be pointed at. ``"local"``
#: targets any OpenAI-compatible server (vLLM, Ollama, LM Studio, or the test
#: mock in ``tests/mock_local_llm.py``).
LLMProvider = Literal["openai", "anthropic", "gemini", "deepseek", "local"]

#: How much reasoning budget to spend. The mode selects a model *tier* per
#: provider (see :func:`graph_library.error_analysis.llm_factory.get_error_analysis_llm`); it
#: never changes the prompt or the deterministic pass.
AnalysisMode = Literal["fast", "standard", "deep"]


class ErrorSignature(TypedDict):
    """One class of error, not one occurrence of it.

    A signature is the unit the LLM reasons about: every record whose masked
    ``template`` is identical collapses into a single entry carrying the count
    and the metadata of the whole group. This is what keeps a 1,500-line error
    storm inside a single prompt.

    Attributes:
        signature_id: Stable id of the form ``"ERR_001"``, assigned by
            descending ``count`` — ``ERR_001`` is always the loudest signature.
            Ids are positional within one investigation and are not stable
            across runs on different payloads.
        template: The masked, whitespace-normalized pattern shared by every
            record in the group (e.g. ``"Failed connection to <IP>:<PORT>"``).
            This is the grouping key.
        severity: The upper-cased level of the records in this group (e.g.
            ``"ERROR"``, ``"CRITICAL"``). Records at different severities never
            share a signature even when their templates match.
        count: Total occurrences in the payload.
        first_seen: Earliest occurrence, as an ISO-8601 timestamp when the
            group carries any usable timestamp, otherwise as ``"line 81"``.
            ``None`` only for a group that somehow carries neither.
        last_seen: Latest occurrence, same representation as ``first_seen``.
        loggers: Unique logger / component names that emitted these records,
            sorted. Records without a logger contribute nothing (no invented
            ``"UNKNOWN"``).
        sample_messages: Up to 2 *unmasked* representative messages, so the LLM
            sees real values (and real stack traces) alongside the template.
        is_root_cause_candidate: Whether this error is a primary trigger rather
            than downstream noise. Set by the LLM; ``False`` after the
            deterministic pass.
        explanation: A concise account of why this error occurred. Set by the
            LLM; ``""`` after the deterministic pass.
    """

    signature_id: str
    template: str
    severity: str
    count: int
    first_seen: str | None
    last_seen: str | None
    loggers: list[str]
    sample_messages: list[str]
    is_root_cause_candidate: bool
    explanation: str


class ErrorSummary(TypedDict):
    """The Error Analysis Node's complete contribution to the graph state.

    Attributes:
        total_errors_analyzed: How many individual records were filtered in and
            fingerprinted — counting every occurrence, including those in
            signatures dropped by the prompt cap.
        unique_signatures_found: How many distinct templates those records
            collapsed into, again counting the dropped ones. When this exceeds
            ``len(signatures)`` the omission is stated in
            ``investigation_notes``, never left silent.
        primary_error_signature_id: The ``signature_id`` the LLM identified as
            the root cause, or ``None`` when it could not single one out (and
            always ``None`` when there was nothing to analyze).
        signatures: The signatures sent to the LLM, ordered by descending
            ``count``, with the LLM's evaluations merged back in.
        cascading_impact_summary: The LLM's account of how the primary error
            propagated into the secondary failures. ``""`` when no analysis ran.
    """

    total_errors_analyzed: int
    unique_signatures_found: int
    primary_error_signature_id: str | None
    signatures: list[ErrorSignature]
    cascading_impact_summary: str


# ---------------------------------------------------------------------------
# LLM structured-output schemas
# ---------------------------------------------------------------------------
# These are Pydantic models (not TypedDicts) because they are passed straight to
# ``with_structured_output()``. Every ``description`` below is part of the
# prompt the provider sees — they are instructions, not comments, and should be
# edited with that in mind.


class LLMErrorSignatureEvaluation(BaseModel):
    """The model's verdict on a single error signature."""

    signature_id: str = Field(
        description="Unique signature ID matching the input error signature."
    )
    is_root_cause_candidate: bool = Field(
        description=(
            "True if this error is a primary trigger rather than downstream "
            "noise."
        )
    )
    explanation: str = Field(
        description="Concise 1-2 sentence explanation of why this error occurred."
    )


class LLMErrorAnalysisResult(_ToolArgumentEnvelope):
    """The model's complete response for one batch of error signatures."""

    primary_error_signature_id: str | None = Field(
        description="The signature_id corresponding to the root cause error."
    )
    cascading_impact_summary: str = Field(
        description=(
            "Brief summary explaining how primary errors led to secondary "
            "failures."
        )
    )
    evaluations: list[LLMErrorSignatureEvaluation] = Field(
        description="LLM evaluation for each provided error signature."
    )


#: How many web-search queries the decision pass may ask for. The cap is small
#: on purpose: each query is a network round trip on the critical path of a node
#: that already runs in parallel with three others, and the signatures worth
#: searching for are the rare ones. Three is enough to cover an error, its
#: framework and its ecosystem without turning the node into a crawler.
MAX_SEARCH_QUERIES = 3


class LLMSearchDecision(_ToolArgumentEnvelope):
    """The model's answer to *"is anything here obscure enough to look up?"*.

    The opening move of the optional two-pass web-search path: pass 1 shows the
    model the signature templates and nothing else, and it replies with the
    queries worth running — or with none at all, which is the expected answer
    for the ordinary connection refusals and null dereferences that make up
    most log payloads.

    An empty ``queries`` list is therefore a *success*, not a failure, and it is
    the only signal the node needs: there is no separate ``needs_search`` flag
    to contradict it.
    """

    queries: list[str] = Field(
        default_factory=list,
        description=(
            "Between 0 and "
            f"{MAX_SEARCH_QUERIES} web-search queries that would return "
            "documentation explaining these errors. Return an empty list when "
            "the errors are common enough that a senior engineer would already "
            "know the cause."
        ),
    )
    reasoning: str = Field(
        default="",
        description=(
            "One sentence on why these errors do or do not warrant an external "
            "lookup."
        ),
    )

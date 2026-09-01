"""The synthesis models produced by the Prepare Output Node.

This module holds the two halves of that node's contract, and they are
deliberately different kinds of object:

    * ``StructuredInvestigationReport`` and its four sections are
      :class:`~typing.TypedDict` s, like every other shared model, so the
      report *is* the plain dict that flows through LangGraph state and lands
      in the database. There is no serialization layer between the node, the
      API and storage;
    * :class:`LLMPrepareOutputResult` is a :class:`~pydantic.BaseModel`,
      because it exists for exactly one purpose — to be handed to
      ``ChatModel.with_structured_output()`` as the response schema. It is a
      transport contract with a provider, never graph state; the node merges
      its two text fields into the report and discards the rest.

The report is partitioned by *provenance* rather than by topic, and that is the
whole design:

    * ``deterministic_outputs`` — arithmetic. Reproducible from the same logs.
    * ``ai_insights`` — what a model concluded, from the two upstream LLM nodes.
    * ``synthesis`` — what this node's own model concluded.
    * ``metadata`` — the run's identity, its ingestion health and the
      deterministic confidence score.

A reader of a stored report can therefore always tell which numbers are facts
and which sentences are inferences, without having to know which node produced
what. That distinction is exactly what gets lost when a report is flattened
into one bag of fields.
"""

from __future__ import annotations

from typing import TypedDict

from pydantic import Field

from .base import _ToolArgumentEnvelope
from .error_analysis import ErrorSummary
from .parser_metrics import ParserMetrics
from .pattern_analysis import PatternSummary
from .statistics import Statistics
from .timeline import TimelineEvent


class StructuredSynthesis(TypedDict):
    """What this node concluded, in the words it will be shown in.

    Attributes:
        root_cause: A single sentence naming the core trigger. Mirrors the
            ``root_cause`` state field.
        executive_summary: The multi-paragraph narrative — incident sequence,
            cascade, and any data-quality caveat. Mirrors the
            ``executive_summary`` state field.
        investigation_notes: The observations the upstream nodes recorded about
            their own limits, snapshotted at the moment this report was built.
            Carried inside the report so a stored investigation explains what
            its numbers could *not* cover without a join against another table.
    """

    root_cause: str
    executive_summary: str
    investigation_notes: list[str]


class StructuredDeterministicOutputs(TypedDict):
    """The reproducible half of the report — arithmetic, not inference.

    Attributes:
        statistics: The Statistics Node's dataset composition, verbatim.
        timeline: The Timeline Node's chronological events, verbatim.
    """

    statistics: Statistics
    timeline: list[TimelineEvent]


class StructuredAIInsights(TypedDict):
    """The upstream model-derived findings, kept apart from this node's own.

    Attributes:
        error_summary: The Error Analysis Node's signatures and root-cause
            nomination, verbatim.
        pattern_summary: The Pattern Analysis Node's behavioral findings,
            verbatim.
    """

    error_summary: ErrorSummary
    pattern_summary: PatternSummary


class StructuredReportMetadata(TypedDict):
    """Who was investigated, with what, and how much to trust the answer.

    Attributes:
        application_name: The application under investigation.
        investigation_timestamp: When the caller ran the investigation, as
            supplied in the input state. ``""`` when the caller omitted it —
            this node never invents a clock reading.
        analysis_mode: The reasoning tier the run actually used.
        llm_provider: The vendor the run actually used.
        confidence_score: The deterministic score from
            :func:`graph_library.prepare_output.compute_confidence_score`, on a
            0-100 integer scale. The same value as the ``confidence_score``
            state field.
        parser_metrics: Ingestion health, verbatim from the parser. Present in
            the report because it is what *qualifies* the confidence score —
            the score alone says "70", these numbers say why.
    """

    application_name: str
    investigation_timestamp: str
    analysis_mode: str
    llm_provider: str
    confidence_score: int
    parser_metrics: ParserMetrics


class StructuredInvestigationReport(TypedDict):
    """The complete machine-readable investigation, ready to persist.

    This is the payload of the ``structured_report`` state field and the shape
    the ``write_to_db`` node stores. It is also what a UI hydrates from, which
    is why every upstream artifact is carried verbatim rather than summarized:
    a client that wants to draw the timeline needs the timeline, not a sentence
    about it.

    Attributes:
        metadata: Run identity, ingestion health and the confidence score.
        synthesis: This node's root cause and executive summary.
        deterministic_outputs: The statistics and timeline, verbatim.
        ai_insights: The error and pattern summaries, verbatim.
    """

    metadata: StructuredReportMetadata
    synthesis: StructuredSynthesis
    deterministic_outputs: StructuredDeterministicOutputs
    ai_insights: StructuredAIInsights


# ---------------------------------------------------------------------------
# LLM structured-output schema
# ---------------------------------------------------------------------------
# A Pydantic model, not a TypedDict, because it is passed straight to
# ``with_structured_output()``. Every ``description`` below is part of the prompt
# the provider sees — they are instructions, not comments, and should be edited
# with that in mind.


class LLMPrepareOutputResult(_ToolArgumentEnvelope):
    """The model's synthesis of every upstream finding.

    Inherits the tool-argument unwrapping from
    :class:`~graph_library.models.base._ToolArgumentEnvelope` for the same
    reason every other response schema in this package does: some providers
    nest the tool-call arguments one level deeper than the schema they were
    given, and a schema whose fields are all required would otherwise fail with
    three "field required" errors against a payload that actually contained
    them.
    """

    root_cause: str = Field(
        description=(
            "Concise 1-sentence primary diagnosis identifying the core trigger."
        )
    )
    executive_summary: str = Field(
        description=(
            "Multi-paragraph narrative combining timeline sequence, cascading "
            "errors, and data quality caveats."
        )
    )
    llm_confidence_score: int = Field(
        description=(
            "Integer 0-100 representing confidence in the diagnosis based "
            "purely on signal clarity."
        )
    )

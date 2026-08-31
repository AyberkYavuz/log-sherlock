"""The behavioral-pattern models produced by the Pattern Analysis Node.

``PatternSummary`` answers a question none of its inputs can answer alone —
*"how did this system behave, and what about that behaviour is abnormal?"*. The
node that produces it sits downstream of the two deterministic nodes rather
than beside them, because the patterns it looks for are properties of their
output: the distributions the Statistics Node computes, and the buckets and
milestones the Timeline Node computes.

The split between the two class families here mirrors
:mod:`graph_library.models.error_analysis` exactly, and for the same reason:

    * ``PatternSummary`` / ``SystemAnomalyRecord`` are :class:`~typing.TypedDict`
      s like every other shared model, so a pattern summary *is* the plain dict
      that flows through LangGraph state — no serialization layer;
    * ``PatternAnalysisResult`` / ``SystemAnomaly`` are
      :class:`~pydantic.BaseModel` s, because they exist for exactly one purpose:
      to be handed to ``ChatModel.with_structured_output()`` as the response
      schema. They are a transport contract with the model, not graph state.

The two families are deliberate mirrors of one another — field for field, name
for name — so ``PatternAnalysisResult.model_dump()`` *is* a ``PatternSummary``
and the node needs no translation layer between them. The deterministic
fallback in :mod:`graph_library.pattern_analysis.fallback` builds the Pydantic
side too, so a degraded run and a healthy one publish the identical shape.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, Field

from .base import _ToolArgumentEnvelope

#: The closed vocabulary of behaviours the node may report. Closed rather than
#: free text because a downstream consumer has to be able to filter and count
#: these, which is impossible when the model invents a new category per run.
#:
#: ``volume_spike``        — a burst of activity or errors well above the
#:                           surrounding baseline.
#: ``logger_cascade``      — a failure in one component followed by failures in
#:                           others, in an order that suggests propagation.
#: ``metadata_clustering`` — failures concentrated in one value of a metadata
#:                           dimension (one endpoint, one tenant, one host).
#: ``baseline_shift``      — a lasting change in the normal operating level,
#:                           as opposed to a transient spike.
AnomalyCategory = Literal[
    "volume_spike",
    "logger_cascade",
    "metadata_clustering",
    "baseline_shift",
]

#: How much attention an anomaly warrants. Three tiers rather than a numeric
#: score: a model cannot calibrate a 0-100 scale consistently across runs, but
#: it can tell "worth knowing" from "worth paging someone".
AnomalySeverity = Literal["info", "warning", "critical"]


class SystemAnomalyRecord(TypedDict):
    """One reported anomaly, as it appears in graph state.

    The TypedDict half of :class:`SystemAnomaly` — see the module docstring for
    why both exist.

    Attributes:
        category: Which kind of behaviour this is, from
            :data:`AnomalyCategory`.
        severity: How much attention it warrants, from :data:`AnomalySeverity`.
        description: A brief account of what was observed.
        affected_loggers: The loggers involved, possibly empty when the anomaly
            is not attributable to particular components.
        time_window: When it happened, as an ISO-8601 instant or a human-readable
            window. ``None`` when the anomaly is a property of the dataset as a
            whole rather than of a moment in it.
    """

    category: AnomalyCategory
    severity: AnomalySeverity
    description: str
    affected_loggers: list[str]
    time_window: str | None


class PatternSummary(TypedDict):
    """Behavioral patterns observed across the statistics and the timeline.

    The payload of the ``pattern_summary`` state field, and the shape the
    recommendation node consumes.

    Attributes:
        anomalies: Individually reportable behaviours, each a
            :class:`SystemAnomalyRecord`. Empty is a valid and common answer —
            a healthy payload has no anomalies, and inventing one to fill the
            list would be worse than an empty one.
        cross_logger_correlations: Connections between failures in different
            components, one sentence each.
        metadata_insights: Takeaways from the metadata distributions — the
            dimensions along which failures concentrate.
        behavioral_synthesis: A narrative of how the system's behaviour evolved
            over the window. ``""`` when there was nothing to narrate.
    """

    anomalies: list[SystemAnomalyRecord]
    cross_logger_correlations: list[str]
    metadata_insights: list[str]
    behavioral_synthesis: str


# ---------------------------------------------------------------------------
# LLM structured-output schemas
# ---------------------------------------------------------------------------
# These are Pydantic models (not TypedDicts) because they are passed straight to
# ``with_structured_output()``. Every ``description`` below is part of the
# prompt the provider sees — they are instructions, not comments, and should be
# edited with that in mind.


class SystemAnomaly(BaseModel):
    """One behaviour the model considers abnormal."""

    category: AnomalyCategory = Field(
        description="Type of system anomaly observed."
    )
    severity: AnomalySeverity = Field(
        description="Severity tier of the anomaly."
    )
    description: str = Field(
        description=(
            "Brief account of the anomaly, in one or two sentences, citing the "
            "counts or timestamps it is based on."
        )
    )
    affected_loggers: list[str] = Field(
        default_factory=list,
        description=(
            "Loggers involved, named exactly as they appear in the input. "
            "Empty when the anomaly is not attributable to specific components."
        ),
    )
    time_window: str | None = Field(
        default=None,
        description=(
            "The timestamp or window the anomaly occurred in, copied from the "
            "timeline. Null when it is a property of the whole dataset."
        ),
    )


class PatternAnalysisResult(_ToolArgumentEnvelope):
    """The model's complete response for one pattern-analysis call.

    Inherits from :class:`~graph_library.models.base._ToolArgumentEnvelope`
    because this schema is bound as a provider tool call like every other
    ``LLM*`` response schema, and the wrapper key some providers add would
    otherwise cost the node its whole analysis.
    """

    anomalies: list[SystemAnomaly] = Field(
        default_factory=list,
        description=(
            "Behaviours that stand out against the rest of the window. Return "
            "an empty list when the system behaved unremarkably — do not invent "
            "an anomaly to fill it."
        ),
    )
    cross_logger_correlations: list[str] = Field(
        default_factory=list,
        description=(
            "Cross-component failure connections, one sentence each, grounded "
            "in the order the loggers appear in the timeline."
        ),
    )
    metadata_insights: list[str] = Field(
        default_factory=list,
        description=(
            "Key takeaways from the metadata distribution clustering — the "
            "dimensions along which activity or failures concentrate."
        ),
    )
    behavioral_synthesis: str = Field(
        description=(
            "Narrative summarizing how overall system behavior evolved across "
            "the window, in three to five sentences."
        )
    )

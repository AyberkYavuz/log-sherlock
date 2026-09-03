"""The HTTP contract — every payload that crosses the network boundary.

These are Pydantic v2 models rather than ``TypedDict`` s, and the distinction is
the same one :mod:`graph_library.models` draws for its ``LLM*`` schemas: a
``TypedDict`` describes a structure that flows through the graph, while these
describe a structure that is *validated at a boundary*. A request arriving from
a browser is untrusted input and must be rejected with a 422 before it reaches a
service; a ``TypedDict`` cannot do that.

Nothing here redefines a graph model. ``structured_report`` is carried as the
``StructuredInvestigationReport`` that :mod:`graph_library.models` already
declares, imported for its type and re-serialized verbatim — a client that wants
to draw the timeline needs the timeline, not this layer's summary of it.

Two design notes worth stating, because both look like omissions:

    * **``analysis_mode`` and ``llm_provider`` are plain ``str``, not
      ``Literal``.** The graph normalizes them itself
      (:func:`~graph_library.error_analysis.llm_factory.normalize_provider`
      resolves ``Claude``, ``google``, ``GPT`` and a table of observed
      misspellings), and a ``Literal`` here would reject inputs the engine
      explicitly supports. Validating them twice, with the stricter copy in
      front, would make this layer the one that decides which providers exist.
    * **The list endpoint never carries ``structured_report``.** A stored report
      runs to megabytes, and the record list is what a UI renders on first
      paint. The two are separate response models precisely so the heavy field
      cannot be added to the light one by accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from graph_library.models import StructuredInvestigationReport

#: The ``investigation_id`` column is ``VARCHAR(255)``. Enforced here so an
#: over-long id is a 422 naming the field rather than a 500 from the driver
#: halfway through a write.
MAX_INVESTIGATION_ID_LENGTH = 255

#: Pagination bounds. The ceiling exists because ``limit`` sizes a database
#: round trip a caller controls: without it, ``limit=100000`` is a denial of
#: service written in JSON.
MIN_PAGE = 1
MIN_LIMIT = 1
MAX_LIMIT = 100
DEFAULT_PAGE = 1
DEFAULT_LIMIT = 10


class _StrictModel(BaseModel):
    """Base for every *request* model: unknown fields are an error.

    ``extra="forbid"`` turns a typo in a client payload into a 422 that names
    the offending key, instead of a silently ignored field and a run that used
    defaults the caller did not intend. A misspelled ``analysis_modes`` is the
    exact mistake this catches, and it is invisible under Pydantic's default.

    Responses deliberately do *not* inherit this — they are constructed by this
    application, not parsed from anywhere.
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Liveness answer for ``GET /api/health``.

    Deliberately shallow: it reports that the process is up and serving, and
    does *not* reach for the database. A health check that fails when Postgres
    is unreachable would take the whole API out of a load balancer over a
    dependency that only two of its five endpoints need.
    """

    status: Literal["ok"] = "ok"
    message: str = "Backend is running"


# ---------------------------------------------------------------------------
# Graph invocation
# ---------------------------------------------------------------------------


class InvestigateRequest(_StrictModel):
    """The body of ``POST /api/investigate``.

    Attributes:
        application_name: The application whose logs these are. Required, and
            required to be non-blank — it is what a stored investigation is
            identified by in a list, and ``""`` would render as an unlabelled
            row.
        raw_logs: The log text itself, not a path. Required and non-blank: an
            empty payload would run all eight nodes to produce an empty report.
        analysis_mode: Reasoning tier — ``fast``, ``standard`` or ``deep``. Free
            text by design; see the module docstring.
        llm_provider: Vendor to reason with. Free text by the same design.
        enable_web_search: Opt in to the error-analysis web-search detour. Off
            by default, matching the graph's own default, because it trades
            latency and cost for coverage of unfamiliar errors.
        investigation_id: The primary key to store under. Optional — one is
            generated when it is absent, which is what makes a run from a UI
            with no id field persist at all.
    """

    application_name: str = Field(min_length=1, max_length=255)
    raw_logs: str = Field(min_length=1)
    analysis_mode: str = Field(default="standard", min_length=1)
    llm_provider: str = Field(default="openai", min_length=1)
    enable_web_search: bool = False
    investigation_id: str | None = Field(
        default=None, min_length=1, max_length=MAX_INVESTIGATION_ID_LENGTH
    )


class InvestigateResponse(BaseModel):
    """The answer to ``POST /api/investigate``.

    Attributes:
        investigation_id: The key this run was stored under — the one the caller
            supplied, or the one that was generated for it. Returned in both
            cases because the client needs it to fetch the report back, and on
            the generated path this response is the only place it appears.
        db_persisted: Whether the report actually reached PostgreSQL. This is
            the field the UI branches on: ``True`` unlocks the record list,
            ``False`` keeps the form on screen.
        investigation_notes: What every node recorded about its own limits,
            including the reason a ``db_persisted: False`` run was not stored.
            Carried here because a ``False`` with no explanation gives a client
            nothing to put in its warning toast — the flag says *that* it
            failed, these say *why*.
    """

    investigation_id: str
    db_persisted: bool
    investigation_notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Record list
# ---------------------------------------------------------------------------


class PaginationParams(_StrictModel):
    """The body of ``POST /api/investigations``.

    Attributes:
        page: 1-based page number.
        limit: Rows per page, at most :data:`MAX_LIMIT`.
    """

    page: int = Field(default=DEFAULT_PAGE, ge=MIN_PAGE)
    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)

    @property
    def offset(self) -> int:
        """The SQL ``OFFSET`` this page starts at."""
        return (self.page - 1) * self.limit


class InvestigationMetadataItem(BaseModel):
    """One row of the record list — everything *except* the report.

    Every field but the id is optional because every column but the primary key
    is nullable in the table. A run that degraded before it could record a
    provider still has a row, and that row still belongs in the list.

    Attributes:
        investigation_id: The primary key.
        application_name: The application investigated.
        confidence_score: The published 0-100 score, or ``None`` when the run
            produced none. ``None`` and ``0`` mean different things — "not
            measured" against "measured as zero" — and are kept distinct all the
            way to the client.
        analysis_mode: The normalized reasoning tier the run used.
        llm_provider: The normalized vendor the run used.
        created_at: When the investigation was first stored.
        updated_at: When it was last re-run under the same id.
    """

    investigation_id: str
    application_name: str | None = None
    confidence_score: int | None = None
    analysis_mode: str | None = None
    llm_provider: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PaginatedInvestigationsResponse(BaseModel):
    """A page of the record list, plus what it takes to page through the rest.

    Attributes:
        items: The rows on this page, newest first.
        total: How many rows exist in total, not on this page.
        page: The page that was served.
        limit: The page size that was served.
        total_pages: How many pages ``total`` divides into at ``limit``. ``0``
            for an empty table, so a client can test it directly rather than
            special-casing "1 page containing nothing".
    """

    items: list[InvestigationMetadataItem]
    total: int
    page: int
    limit: int
    total_pages: int


# ---------------------------------------------------------------------------
# Record detail
# ---------------------------------------------------------------------------


class InvestigationDetailRequest(_StrictModel):
    """The body of ``POST /api/investigations/{id}``.

    Every field is optional and there are none, which is deliberate: the
    endpoint is a POST carrying no parameters today, and declaring the model now
    means a future filter (``sections=["synthesis"]``) is an added field rather
    than a changed signature. ``{}`` and an omitted body both validate.
    """


class InvestigationDetailResponse(BaseModel):
    """The full stored investigation.

    Attributes:
        investigation_id: The primary key, echoed back so a client holding
            several in flight can match a response to its request.
        structured_report: The complete
            :class:`~graph_library.models.StructuredInvestigationReport`,
            verbatim from the ``JSONB`` column — ``metadata``, ``synthesis``,
            ``deterministic_outputs`` and ``ai_insights``. Nothing is summarized
            or reshaped on the way out.
    """

    investigation_id: str
    # Typed as the shared TypedDict for documentation and OpenAPI, and *not*
    # validated field by field on the way out: the report was written by the
    # graph and read back from JSONB, so re-validating it here would only mean
    # that a report stored by an older release fails to be served at all.
    structured_report: StructuredInvestigationReport | dict[str, Any]


class DeleteInvestigationResponse(BaseModel):
    """The answer to ``DELETE /api/investigations/{id}``."""

    status: Literal["deleted"] = "deleted"
    investigation_id: str


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """The shape of every error this API returns.

    One field, matching FastAPI's own ``HTTPException`` envelope, so a client
    reads ``detail`` on a 404, a 422 and a 500 alike rather than branching on
    the status code to find out where the message is.
    """

    detail: Any


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_PAGE",
    "MAX_INVESTIGATION_ID_LENGTH",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "MIN_PAGE",
    "DeleteInvestigationResponse",
    "ErrorResponse",
    "HealthResponse",
    "InvestigateRequest",
    "InvestigateResponse",
    "InvestigationDetailRequest",
    "InvestigationDetailResponse",
    "InvestigationMetadataItem",
    "PaginatedInvestigationsResponse",
    "PaginationParams",
]

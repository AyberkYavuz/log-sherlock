"""The four investigation endpoints.

Every handler here is three lines: resolve a dependency, call a service, return
what it returns. That is the point — the decisions live in the service layer,
and a route that grew a conditional would be a route that had started making
them.

**Why some handlers are ``def`` and one is ``async def``.** Starlette runs a
plain ``def`` handler on a worker thread and an ``async def`` handler on the
event loop. The three storage endpoints do blocking socket I/O through
``psycopg2``, so they are declared ``def`` and Starlette keeps the loop free for
everything else; declaring them ``async`` would stall every concurrent request
for the duration of a query. ``POST /api/investigate`` is ``async def`` because
the graph is awaited — LangGraph runs the sync node functions on its own workers
while the loop stays responsive, which matters when a single call holds a
connection open for minutes.

**Why the reads are POST.** ``POST /api/investigations`` and
``POST /api/investigations/{id}`` are reads and would conventionally be GETs.
They are POSTs because the client contract specifies a JSON request body for
each, and a GET with a body is unspecified territory that proxies and browser
fetch implementations handle inconsistently.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Path, status

from ..dependencies import GraphRunnerServiceDep, InvestigationServiceDep
from ..schemas import (
    MAX_INVESTIGATION_ID_LENGTH,
    DeleteInvestigationResponse,
    ErrorResponse,
    InvestigateRequest,
    InvestigateResponse,
    InvestigationDetailRequest,
    InvestigationDetailResponse,
    PaginatedInvestigationsResponse,
    PaginationParams,
)

router = APIRouter(tags=["investigations"])

#: The ``{id}`` path parameter, declared once. Bounded to the column width so an
#: over-long key is a 422 naming the parameter rather than a driver error, and
#: floored at 1 so ``/api/investigations/`` cannot resolve to a lookup for the
#: empty string.
_INVESTIGATION_ID = Path(
    description="The investigation's primary key.",
    min_length=1,
    max_length=MAX_INVESTIGATION_ID_LENGTH,
)

#: Documented failure modes shared by the endpoints that touch storage. Listed
#: on the routes so they appear in the OpenAPI schema, which is what a client
#: generator and the Swagger page read.
_STORAGE_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No investigation exists with that id.",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "The investigations database could not be reached.",
    },
}


@router.post(
    "/investigate",
    response_model=InvestigateResponse,
    status_code=status.HTTP_200_OK,
    summary="Run a new investigation through the LangGraph pipeline",
    responses={
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "The request body failed validation.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "The analysis pipeline could not be run.",
        },
        status.HTTP_504_GATEWAY_TIMEOUT: {
            "model": ErrorResponse,
            "description": "The analysis exceeded the configured deadline.",
        },
    },
)
async def investigate(
    request: InvestigateRequest,
    service: GraphRunnerServiceDep,
) -> InvestigateResponse:
    """Analyze a log payload and store the result.

    Runs the whole pipeline — parse, statistics, timeline, pattern analysis,
    error analysis with an optional web search, synthesis and persistence — and
    reports where the result went.

    This can take a long time. Every node degrades rather than fails, so a
    provider that is unreachable costs the investigation its interpretation
    rather than costing the caller a 5xx.

    Args:
        request: What to analyze and how.
        service: Injected graph runner.

    Returns:
        The investigation id, whether the report reached PostgreSQL, and the
        notes every node recorded. A ``db_persisted`` of ``False`` is still a
        200: the analysis ran, and ``investigation_notes`` says why it was not
        stored.
    """
    return await service.execute(request)


@router.post(
    "/investigations",
    response_model=PaginatedInvestigationsResponse,
    status_code=status.HTTP_200_OK,
    summary="List stored investigations, newest first",
    responses={
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "The pagination parameters failed validation.",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: _STORAGE_RESPONSES[
            status.HTTP_503_SERVICE_UNAVAILABLE
        ],
    },
)
def list_investigations(
    service: InvestigationServiceDep,
    params: PaginationParams | None = Body(default=None),
) -> PaginatedInvestigationsResponse:
    """Return one page of investigation metadata.

    Metadata only — ``structured_report`` is deliberately absent, because a
    stored report runs to megabytes and this is what a UI renders on first
    paint. Fetch the report for one record with
    ``POST /api/investigations/{id}``.

    Args:
        service: Injected investigation service.
        params: Page and limit. An omitted body is the same as ``{}`` and yields
            the first page at the default size.

    Returns:
        The page, the total row count, and how many pages that divides into.
    """
    page = params or PaginationParams()
    return service.fetch(page=page.page, limit=page.limit)


@router.post(
    "/investigations/{investigation_id}",
    response_model=InvestigationDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Fetch one investigation's full structured report",
    responses=_STORAGE_RESPONSES,
)
def get_investigation(
    service: InvestigationServiceDep,
    investigation_id: str = _INVESTIGATION_ID,
    _body: InvestigationDetailRequest | None = Body(default=None),
) -> InvestigationDetailResponse:
    """Return the complete stored report for one investigation.

    Args:
        service: Injected investigation service.
        investigation_id: The primary key to look up.
        _body: Accepted and unused. The endpoint takes no parameters today; the
            body is declared so that adding one later is a new field rather than
            a changed method.

    Returns:
        The id and the whole ``StructuredInvestigationReport`` — ``metadata``,
        ``synthesis``, ``deterministic_outputs`` and ``ai_insights`` — verbatim
        from the JSONB column.

    Raises:
        InvestigationNotFoundError: Rendered as a 404 when no such row exists.
    """
    return service.fetch(investigation_id)


@router.delete(
    "/investigations/{investigation_id}",
    response_model=DeleteInvestigationResponse,
    status_code=status.HTTP_200_OK,
    summary="Delete one stored investigation",
    responses=_STORAGE_RESPONSES,
)
def delete_investigation(
    service: InvestigationServiceDep,
    investigation_id: str = _INVESTIGATION_ID,
) -> DeleteInvestigationResponse:
    """Remove one investigation from storage.

    Args:
        service: Injected investigation service.
        investigation_id: The primary key to remove.

    Returns:
        The deletion receipt.

    Raises:
        InvestigationNotFoundError: Rendered as a 404 when there was no such row
            to delete. Reported rather than treated as success, because a client
            deleting something it could not see is looking at a stale list.
    """
    return service.remove(investigation_id)


__all__ = ["router"]

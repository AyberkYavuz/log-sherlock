"""The backend's error taxonomy, and the handlers that render it as JSON.

Services and repositories raise the exceptions below; they never build an HTTP
response. That separation is what keeps the service layer callable from a
script, a test or a future worker without dragging FastAPI along — a repository
that raised :class:`~fastapi.HTTPException` would be a repository that only
works inside a web request.

The handlers at the bottom are the single place where an exception becomes a
status code, so the mapping is reviewable as a table rather than scattered
across five route functions.

Every response body is ``{"detail": ...}``, matching FastAPI's own envelope, so
a client reads one field regardless of which layer failed.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class BackendError(Exception):
    """Base for every failure this backend raises deliberately.

    Carrying a base class means the generic handler can tell "a condition this
    code anticipated" from "something nobody expected", and report the two
    differently: the first is a message worth showing a user, the second is a
    traceback worth logging and a deliberately vague sentence worth returning.
    """

    #: The status code this failure maps to. Overridden per subclass.
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class InvestigationNotFoundError(BackendError):
    """No row exists for the requested ``investigation_id``."""

    status_code = status.HTTP_404_NOT_FOUND

    def __init__(self, investigation_id: str) -> None:
        super().__init__("Investigation not found")
        self.investigation_id = investigation_id


class RepositoryError(BackendError):
    """The database could not be reached, or a statement failed.

    Mapped to 503 rather than 500 because the two mean different things to a
    caller: 500 says this request is broken and retrying is pointless, 503 says
    the dependency is down and retrying later is exactly right. An unreachable
    Postgres is the second.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE


class GraphExecutionError(BackendError):
    """The LangGraph pipeline could not be run to completion.

    Rare by construction: every node in the graph degrades rather than raises,
    so this covers the failures *outside* that contract — the graph could not be
    compiled, or the run exceeded the configured deadline.
    """

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR


class GraphTimeoutError(GraphExecutionError):
    """The graph exceeded :attr:`~backend.config.ApiSettings.graph_timeout`.

    504 rather than 500: nothing is wrong with the request, and the same payload
    may well succeed against a faster provider or a smaller log file. A client
    that reads 500 as "do not retry this" would be wrong here.
    """

    status_code = status.HTTP_504_GATEWAY_TIMEOUT


def _json(status_code: int, detail: Any) -> JSONResponse:
    """Render one error envelope."""
    return JSONResponse(status_code=status_code, content={"detail": detail})


async def _handle_backend_error(
    _request: Request, exc: BackendError
) -> JSONResponse:
    """Render a deliberate failure at the status code its class declares."""
    # ``warning`` rather than ``error``: every one of these is a condition the
    # code anticipated and reported cleanly. A 404 for a deleted investigation
    # is not an incident.
    logger.warning("%s: %s", type(exc).__name__, exc.detail)
    return _json(exc.status_code, exc.detail)


async def _handle_http_exception(
    _request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Render an ``HTTPException`` raised directly by a route or by Starlette.

    Registered so that a 404 from an unmatched *path* comes back in the same
    envelope as a 404 from a missing record, rather than in Starlette's plain
    ``{"detail": "Not Found"}`` default with different headers.
    """
    return _json(exc.status_code, exc.detail)


async def _handle_validation_error(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Render a 422 for a body that does not satisfy its schema.

    FastAPI's own handler returns 422 already; this one exists to guarantee the
    envelope. ``jsonable_encoder`` is required rather than cosmetic — a
    validation error's ``ctx`` can hold the original ``ValueError``, which is
    not JSON-serializable and would turn a 422 into a 500 inside the serializer.
    """
    logger.info("Request validation failed: %s", exc.errors())
    return _json(status.HTTP_422_UNPROCESSABLE_ENTITY, jsonable_encoder(exc.errors()))


async def _handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    """Render anything that reached the top of the stack uncaught.

    The traceback goes to the log and a fixed sentence goes to the client. A
    stack trace in an HTTP body is an information leak — it names file paths,
    library versions and sometimes a connection string — and it is useless to
    the browser that receives it.
    """
    logger.error("Unhandled error while serving a request", exc_info=exc)
    return _json(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "Internal server error. See the server logs for details.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler above to ``app``.

    Order does not matter — Starlette dispatches on the exception class, taking
    the most specific registered match — but the grouping below reads as the
    mapping it is.

    Args:
        app: The application to attach to.
    """
    app.add_exception_handler(BackendError, _handle_backend_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _handle_unexpected_error)


__all__ = [
    "BackendError",
    "GraphExecutionError",
    "GraphTimeoutError",
    "InvestigationNotFoundError",
    "RepositoryError",
    "register_exception_handlers",
]

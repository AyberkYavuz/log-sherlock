"""Reading stored investigations — the service interface and its default form.

The service is the layer that turns *rows* into *responses*. That is the whole
of its job, and keeping it a separate layer earns two things: a route handler
never contains arithmetic (``total_pages`` is computed once, here, not in three
places), and the repository never learns what the wire format looks like.

:meth:`InvestigationService.fetch` is deliberately overloaded rather than split
into two names. The two calls answer the same question — "give me stored
investigations" — and differ only in how the caller identifies what it wants: by
primary key, or by position in the list. ``typing.overload`` is what lets one
name carry both while a type checker still knows that passing an id yields a
detail response and passing a page yields a paginated one. A single ``fetch``
returning a union would push that discrimination onto every caller.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from typing import overload, override

from ..errors import InvestigationNotFoundError
from ..persistence import InvestigationMetadataRow, InvestigationRepository
from ..schemas import (
    DEFAULT_LIMIT,
    DEFAULT_PAGE,
    DeleteInvestigationResponse,
    InvestigationDetailResponse,
    InvestigationMetadataItem,
    PaginatedInvestigationsResponse,
)

logger = logging.getLogger(__name__)


class InvestigationService(ABC):
    """Read and delete stored investigations, in the API's own vocabulary."""

    # -- fetch: one overloaded entry point, two ways to identify a target ----

    @overload
    def fetch(self, investigation_id: str, /) -> InvestigationDetailResponse:
        """Fetch one investigation in full, by primary key."""

    @overload
    def fetch(
        self, /, *, page: int = ..., limit: int = ...
    ) -> PaginatedInvestigationsResponse:
        """Fetch one page of record metadata."""

    @abstractmethod
    def fetch(
        self,
        investigation_id: str | None = None,
        /,
        *,
        page: int | None = None,
        limit: int | None = None,
    ) -> InvestigationDetailResponse | PaginatedInvestigationsResponse:
        """Fetch stored investigations, by id or by page.

        Args:
            investigation_id: Positional-only. When given, the single
                investigation to return in full.
            page: 1-based page number, when listing.
            limit: Rows per page, when listing.

        Returns:
            An :class:`~backend.schemas.InvestigationDetailResponse` when an id
            was given, otherwise a
            :class:`~backend.schemas.PaginatedInvestigationsResponse`.

        Raises:
            InvestigationNotFoundError: If an id was given and no such row
                exists.
            RepositoryError: If the store cannot be read.
            TypeError: If both an id and pagination arguments are supplied — the
                two overloads are alternatives, not a combination, and silently
                honouring one would return a shape the caller did not ask for.
        """

    @abstractmethod
    def remove(self, investigation_id: str) -> DeleteInvestigationResponse:
        """Delete one investigation.

        Args:
            investigation_id: The primary key to remove.

        Returns:
            The deletion receipt.

        Raises:
            InvestigationNotFoundError: If no such row exists. Deleting a row
                that was never there is reported rather than treated as success:
                a client that just deleted something it could not see is looking
                at a stale list, and telling it so is what prompts a refresh.
            RepositoryError: If the store cannot be written.
        """


class DefaultInvestigationService(InvestigationService):
    """The one implementation, over any :class:`InvestigationRepository`."""

    def __init__(self, repository: InvestigationRepository) -> None:
        """Bind the service to a repository.

        Args:
            repository: Where investigations are stored. Injected rather than
                constructed, which is what lets the same service run against
                PostgreSQL in production and against a double in a test.
        """
        self._repository = repository

    @overload
    def fetch(self, investigation_id: str, /) -> InvestigationDetailResponse: ...

    @overload
    def fetch(
        self, /, *, page: int = ..., limit: int = ...
    ) -> PaginatedInvestigationsResponse: ...

    @override
    def fetch(
        self,
        investigation_id: str | None = None,
        /,
        *,
        page: int | None = None,
        limit: int | None = None,
    ) -> InvestigationDetailResponse | PaginatedInvestigationsResponse:
        if investigation_id is not None:
            if page is not None or limit is not None:
                raise TypeError(
                    "fetch() takes either an investigation_id or page/limit, "
                    "not both"
                )
            return self._fetch_one(investigation_id)

        return self._fetch_page(
            page=DEFAULT_PAGE if page is None else page,
            limit=DEFAULT_LIMIT if limit is None else limit,
        )

    @override
    def remove(self, investigation_id: str) -> DeleteInvestigationResponse:
        if not self._repository.delete(investigation_id):
            raise InvestigationNotFoundError(investigation_id)

        logger.info("Deleted investigation %r", investigation_id)
        return DeleteInvestigationResponse(investigation_id=investigation_id)

    # -- internals ----------------------------------------------------------

    def _fetch_one(self, investigation_id: str) -> InvestigationDetailResponse:
        """Read one full investigation, or raise."""
        stored = self._repository.fetch_one(investigation_id)
        if stored is None:
            raise InvestigationNotFoundError(investigation_id)

        report = stored["structured_report"]
        logger.info(
            "Served investigation %r (report sections: %s)",
            investigation_id,
            sorted(report) or "<empty>",
        )
        return InvestigationDetailResponse(
            investigation_id=stored["investigation_id"],
            structured_report=report,
        )

    def _fetch_page(
        self, *, page: int, limit: int
    ) -> PaginatedInvestigationsResponse:
        """Read one page of metadata and describe where it sits in the whole."""
        result = self._repository.fetch_page(limit=limit, offset=(page - 1) * limit)
        total = result["total"]

        logger.info(
            "Served page %d (limit %d) of %d investigation(s)", page, limit, total
        )
        return PaginatedInvestigationsResponse(
            items=[self._to_item(row) for row in result["rows"]],
            total=total,
            page=page,
            limit=limit,
            # ``ceil`` over integer division so a partial last page counts, and
            # ``0`` for an empty table rather than ``1``: a client can then test
            # ``total_pages`` directly instead of special-casing "one page that
            # happens to contain nothing".
            total_pages=math.ceil(total / limit) if total else 0,
        )

    @staticmethod
    def _to_item(row: InvestigationMetadataRow) -> InvestigationMetadataItem:
        """Render one row as a list item.

        A straight field-by-field construction rather than ``model_validate``,
        so a column added to the table does not silently become part of the wire
        format before anyone has decided it should be.
        """
        return InvestigationMetadataItem(
            investigation_id=row["investigation_id"],
            application_name=row["application_name"],
            confidence_score=row["confidence_score"],
            analysis_mode=row["analysis_mode"],
            llm_provider=row["llm_provider"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


__all__ = [
    "DefaultInvestigationService",
    "InvestigationService",
]

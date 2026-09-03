"""Data access for stored investigations — the interface and its Postgres form.

:class:`InvestigationRepository` is the abstract boundary between the service
layer and storage. It exists so that "what the API needs from a database" is
stated in three methods rather than implied by SQL scattered through route
handlers, and so a test can satisfy it without a server running.

The interface speaks in plain rows (``TypedDict``) rather than in response
models. A repository that returned
:class:`~backend.schemas.PaginatedInvestigationsResponse` would be a repository
that knows about HTTP, and the shape of the wire format would then be dictated
by the shape of a ``SELECT``. Translating rows into responses is the service's
job.

Failures are translated once, here: every driver exception becomes a
:class:`~backend.errors.RepositoryError`. The layers above therefore never
import ``psycopg2``, and swapping the storage engine does not change a single
``except`` clause upstream.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, TypedDict, override

from graph_library.write_to_db import DatabaseConfig

from ..errors import RepositoryError
from .connection import connection
from .queries import (
    COUNT_INVESTIGATIONS_SQL,
    DELETE_INVESTIGATION_SQL,
    LIST_METADATA_SQL,
    METADATA_COLUMNS,
    SELECT_INVESTIGATION_SQL,
)

logger = logging.getLogger(__name__)


class InvestigationMetadataRow(TypedDict):
    """One row of the record list, exactly as the columns come back.

    Every value but the id may be ``None`` because every column but the primary
    key is nullable. Nothing is defaulted on the way out of the database — a
    ``confidence_score`` of ``None`` means "not measured" and must not be
    rendered as ``0`` here or anywhere above.
    """

    investigation_id: str
    application_name: str | None
    confidence_score: int | None
    analysis_mode: str | None
    llm_provider: str | None
    created_at: Any
    updated_at: Any


class StoredInvestigation(TypedDict):
    """One full stored investigation.

    Attributes:
        investigation_id: The primary key, read back from the row rather than
            echoed from the query parameter.
        structured_report: The ``JSONB`` document, which ``psycopg2`` has
            already decoded into a dict. ``{}`` when the column is ``NULL`` —
            a row that exists with no report is not the same thing as a row that
            does not exist, and only the second is a 404.
    """

    investigation_id: str
    structured_report: dict[str, Any]


class InvestigationPage(TypedDict):
    """One page of the record list, with the total it was drawn from.

    Both halves come from the same connection and the same transaction, so the
    count cannot describe a table the rows were not taken from — the pager a
    client renders always agrees with the rows beside it.
    """

    rows: list[InvestigationMetadataRow]
    total: int


class InvestigationRepository(ABC):
    """Read and delete access to stored investigations.

    Deliberately has no ``create``. The API never writes a row itself: it
    invokes the graph, and the ``write_to_db`` node performs the idempotent
    upsert. Adding a write method here would create a second path to the same
    table with its own opinion about what a report looks like.
    """

    @abstractmethod
    def fetch_page(self, *, limit: int, offset: int) -> InvestigationPage:
        """Read one page of record metadata, newest first.

        Args:
            limit: Maximum rows to return.
            offset: Rows to skip.

        Returns:
            The rows and the total row count. An ``offset`` past the end yields
            an empty ``rows`` list and the real ``total``, which is what lets a
            client recover from asking for a page that no longer exists.

        Raises:
            RepositoryError: If the store cannot be read.
        """

    @abstractmethod
    def fetch_one(self, investigation_id: str) -> StoredInvestigation | None:
        """Read one full investigation.

        Args:
            investigation_id: The primary key to look up.

        Returns:
            The stored investigation, or ``None`` when no such row exists.
            ``None`` means *absent*; a row whose report column is ``NULL`` comes
            back present with an empty report.

        Raises:
            RepositoryError: If the store cannot be read.
        """

    @abstractmethod
    def delete(self, investigation_id: str) -> bool:
        """Remove one investigation.

        Args:
            investigation_id: The primary key to remove.

        Returns:
            ``True`` if a row was removed, ``False`` if there was none to
            remove.

        Raises:
            RepositoryError: If the store cannot be written.
        """


class PostgresInvestigationRepository(InvestigationRepository):
    """The PostgreSQL implementation, over the ``investigations`` table.

    Stateless apart from its configuration: a connection is opened per call and
    closed before it returns. That is the right trade for this workload —
    requests are infrequent and long-lived relative to a connect, and the
    alternative (a pool held across an application whose slowest endpoint blocks
    for minutes) buys latency this API does not need at the cost of state it
    would have to manage.
    """

    def __init__(self, config: DatabaseConfig) -> None:
        """Store the connection settings.

        Args:
            config: Where to connect and as whom. Passed in rather than read
                from the environment here, so the factory owns that decision and
                a test can point one repository somewhere harmless.
        """
        self._config = config

    @property
    def target(self) -> str:
        """A ``host:port/dbname`` label safe to log — it carries no credential."""
        return self._config.target

    @override
    def fetch_page(self, *, limit: int, offset: int) -> InvestigationPage:
        with self._guard("read the investigation list"):
            with connection(self._config) as conn, conn.cursor() as cursor:
                # Count first, in the same transaction as the page below, so the
                # two describe the same table even if a concurrent run inserts
                # between them.
                cursor.execute(COUNT_INVESTIGATIONS_SQL)
                row = cursor.fetchone()
                total = int(row[0]) if row else 0

                cursor.execute(LIST_METADATA_SQL, (limit, offset))
                rows = [self._metadata_row(record) for record in cursor.fetchall()]

        logger.debug(
            "Read %d of %d investigation(s) (limit=%d offset=%d) from %s",
            len(rows),
            total,
            limit,
            offset,
            self.target,
        )
        return InvestigationPage(rows=rows, total=total)

    @override
    def fetch_one(self, investigation_id: str) -> StoredInvestigation | None:
        with self._guard(f"read investigation {investigation_id!r}"):
            with connection(self._config) as conn, conn.cursor() as cursor:
                cursor.execute(SELECT_INVESTIGATION_SQL, (investigation_id,))
                record = cursor.fetchone()

        if record is None:
            logger.debug("No investigation %r in %s", investigation_id, self.target)
            return None

        stored_id, report = record
        return StoredInvestigation(
            investigation_id=str(stored_id),
            # ``NULL`` becomes ``{}`` rather than ``None``: the column is
            # nullable, and a client that asked for a report should receive an
            # empty document it can render as "nothing stored" instead of a
            # ``null`` its type definitions do not allow.
            structured_report=report if isinstance(report, dict) else {},
        )

    @override
    def delete(self, investigation_id: str) -> bool:
        with self._guard(f"delete investigation {investigation_id!r}"):
            with connection(self._config) as conn, conn.cursor() as cursor:
                cursor.execute(DELETE_INVESTIGATION_SQL, (investigation_id,))
                deleted = cursor.rowcount

        # ``rowcount`` is what makes this a single round trip: no SELECT to
        # check existence first, and therefore no window in which another
        # request removes the row between the check and the delete.
        logger.info(
            "Deleted %d row(s) for investigation %r from %s",
            deleted,
            investigation_id,
            self.target,
        )
        return bool(deleted)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _metadata_row(record: tuple[Any, ...]) -> InvestigationMetadataRow:
        """Zip one result tuple onto its column names.

        ``strict=True`` because a mismatch between the projection and
        :data:`~backend.persistence.queries.METADATA_COLUMNS` is a bug that
        would otherwise surface as two same-typed values silently swapped, which
        no test on the values themselves would catch.
        """
        return dict(zip(METADATA_COLUMNS, record, strict=True))  # type: ignore[return-value]

    @staticmethod
    def _guard(action: str) -> Any:
        """A context manager turning any driver failure into a ``RepositoryError``.

        Args:
            action: What was being attempted, phrased to complete the sentence
                "could not <action>" — the message a client eventually reads.

        Returns:
            A context manager. Written as a nested class rather than with
            ``@contextmanager`` so it can re-raise while preserving the original
            as ``__cause__``, which is what puts the driver's own message in the
            logged traceback.
        """

        class _Guard:
            def __enter__(self) -> None:
                return None

            def __exit__(
                self, exc_type: Any, exc: BaseException | None, tb: Any
            ) -> bool:
                if exc is None:
                    return False
                logger.error("Could not %s", action, exc_info=exc)
                raise RepositoryError(
                    f"Could not {action}: the investigations database is "
                    f"unavailable ({type(exc).__name__})."
                ) from exc

        return _Guard()


__all__ = [
    "InvestigationMetadataRow",
    "InvestigationPage",
    "InvestigationRepository",
    "PostgresInvestigationRepository",
    "StoredInvestigation",
]

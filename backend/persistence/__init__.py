"""Storage access for the LogSherlock API.

Laid out by concern, the way the feature packages under ``graph_library/`` are:

    * :mod:`backend.persistence.queries` — every SQL statement the API issues,
    * :mod:`backend.persistence.connection` — connection handling,
    * :mod:`backend.persistence.repository` — the
      :class:`~backend.persistence.repository.InvestigationRepository` interface
      and its PostgreSQL implementation.

The package reads and deletes; it never inserts. Writing an investigation is the
``write_to_db`` node's job, reached by invoking the graph, so there is exactly
one statement in the system that creates a row.
"""

from __future__ import annotations

from .connection import connect, connection
from .queries import (
    COUNT_INVESTIGATIONS_SQL,
    DELETE_INVESTIGATION_SQL,
    LIST_METADATA_SQL,
    METADATA_COLUMNS,
    SELECT_INVESTIGATION_SQL,
    TABLE_NAME,
)
from .repository import (
    InvestigationMetadataRow,
    InvestigationPage,
    InvestigationRepository,
    PostgresInvestigationRepository,
    StoredInvestigation,
)

__all__ = [
    "COUNT_INVESTIGATIONS_SQL",
    "DELETE_INVESTIGATION_SQL",
    "LIST_METADATA_SQL",
    "METADATA_COLUMNS",
    "SELECT_INVESTIGATION_SQL",
    "TABLE_NAME",
    "InvestigationMetadataRow",
    "InvestigationPage",
    "InvestigationRepository",
    "PostgresInvestigationRepository",
    "StoredInvestigation",
    "connect",
    "connection",
]

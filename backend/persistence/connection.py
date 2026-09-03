"""Connection handling for the API's read path.

Deliberately *not* a reuse of :func:`graph_library.write_to_db.db.connection`,
and the reason is worth stating because reuse would otherwise be the obvious
call. That helper routes through
:func:`~graph_library.write_to_db.db.announce`, which prints to stdout as well
as logging — correct for a graph node, whose stdout LangGraph Server surfaces
directly and which runs once per investigation. This module runs once per HTTP
request, and a server that prints two lines every time a browser paints a list
is a server whose logs are unreadable.

What is *not* duplicated is the part that matters: where the database is and as
whom to connect stays in
:class:`~graph_library.write_to_db.config.DatabaseConfig`, so the API cannot
drift onto a different database from the node that writes to it.

``psycopg2`` is imported lazily here for the same reason it is there: this
package is reachable from the application factory, and a module-scope import
would make the driver a hard requirement of *building* the app rather than of
using the three endpoints that touch storage.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from graph_library.write_to_db import DatabaseConfig

logger = logging.getLogger(__name__)


def connect(config: DatabaseConfig) -> Any:
    """Open a connection, importing the driver on the way.

    Args:
        config: Where to connect and as whom.

    Returns:
        An open ``psycopg2`` connection. The caller owns closing it;
        :func:`connection` is the wrapper that does.

    Raises:
        ImportError: If ``psycopg2`` is not installed, with the install command
            in the message.
        Exception: Whatever the driver raises for an unreachable server, a
            rejected credential or a missing database. Unhandled on purpose —
            the repository translates it into a
            :class:`~backend.errors.RepositoryError`, and a helper that
            swallowed it would take the reason away.
    """
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ImportError(
            "psycopg2 is required to read investigations "
            "(pip install psycopg2-binary)"
        ) from exc

    logger.debug("Connecting to Postgres at %s as %s", config.target, config.user)
    return psycopg2.connect(**config.connection_kwargs())


@contextmanager
def connection(config: DatabaseConfig) -> Iterator[Any]:
    """An open connection that commits on success and rolls back on failure.

    ``psycopg2``'s own connection context manager wraps the *transaction* and
    leaves the socket open, which under a long-lived server leaks one connection
    per request. The ``finally`` below is what makes that impossible.

    The commit is not ceremony on a read-only path: ``psycopg2`` opens a
    transaction on the first statement whatever that statement is, and a
    connection closed without ending it leaves an ``idle in transaction`` entry
    behind on the server for as long as the pool holds it.

    Yields:
        The open connection.
    """
    conn = connect(config)
    try:
        yield conn
        conn.commit()
    except Exception:
        # Explicit rather than left to the close below, so the rollback is
        # ordered *before* the exception propagates and the log records the
        # cause rather than a bare disconnect.
        conn.rollback()
        raise
    finally:
        conn.close()


__all__ = ["connect", "connection"]

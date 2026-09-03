"""Connection handling and the two operations built on it.

Everything that touches ``psycopg2`` lives here, and it is imported *lazily* —
inside the function that needs it rather than at module scope. That is the same
rule :mod:`graph_library.error_analysis.llm_factory` applies to the provider
SDKs, and it matters more here: this module is reachable from ``graph.py``
through the node registry, so a top-level import would make the driver a hard
requirement of building the graph at all. A deployment that never persists
anything would fail to start.

Two callers, two entry points:

    * :func:`initialize_database` — used by the root ``init_db.py`` to create or
      empty the table before a run;
    * :func:`upsert_investigation` — used by the node to store one report.

Neither swallows an exception. Failure is the node's decision to absorb and the
script's decision to report, and a helper that returned ``False`` on error would
take the reason away from both.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .config import DatabaseConfig
from .queries import (
    CREATE_TABLE_SQL,
    TABLE_EXISTS_SQL,
    TABLE_NAME,
    TRUNCATE_TABLE_SQL,
    UPSERT_SQL,
)

logger = logging.getLogger(__name__)

#: Prefix on every line this package prints. Chosen so a `grep` over a
#: LangGraph Server log isolates the persistence step from the seven nodes that
#: ran before it.
LOG_PREFIX = "[LogSherlock DB]"


def announce(message: str) -> None:
    """Report progress to both the logger and stdout.

    Both, deliberately. The logger is what a configured deployment captures,
    but LangGraph Server and the CLI show a node's stdout directly, and a
    persistence step that reports nothing there looks identical to one that
    never ran. ``flush`` because the process may be terminated between the last
    node and the interpreter's exit, which is exactly when a buffered final
    line would be lost.
    """
    logger.info("%s %s", LOG_PREFIX, message)
    print(f"{LOG_PREFIX} {message}", flush=True)


def load_env_file() -> None:
    """Load ``.env`` into the environment, if ``python-dotenv`` is installed.

    A no-op when the package is absent or the file does not exist, because the
    variables may perfectly well be exported by the shell, by a Compose
    ``environment:`` block or by a secrets manager — none of which involve a
    file. ``override=False`` is the default and is what this relies on: a real
    credential already in the environment always wins over a checked-in
    placeholder.

    Never raises. Locating and reading a file is the one step here that can
    fail for reasons that have nothing to do with the database — an unreadable
    file, a permission boundary — and none of them are worth failing a
    persistence step that has perfectly good environment variables already.
    """
    try:
        from dotenv import find_dotenv, load_dotenv

        # ``usecwd`` because the default search starts at the *calling
        # module's* directory, which for this file is inside the package. A
        # caller run from the project root would otherwise silently pick up no
        # file at all and fall through to every default in ``DatabaseConfig``.
        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:  # pragma: no cover - optional dependency
        logger.debug("python-dotenv is not installed; reading the environment as-is")
    except Exception:  # noqa: BLE001 - the environment may already be complete
        logger.warning("Could not read a .env file; reading the environment as-is")


def connect(config: DatabaseConfig) -> Any:
    """Open a connection, importing the driver on the way.

    Args:
        config: Where to connect and as whom.

    Returns:
        An open ``psycopg2`` connection. The caller owns closing it;
        :func:`connection` is the wrapper that does.

    Raises:
        ImportError: If ``psycopg2`` is not installed. Raised rather than
            handled so the caller can report it as the configuration problem it
            is — the node degrades on it like any other failure, and
            ``init_db.py`` prints the install command.
        Exception: Whatever the driver raises for an unreachable server, a bad
            credential or a missing database.
    """
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ImportError(
            "psycopg2 is required to persist investigations "
            "(pip install psycopg2-binary)"
        ) from exc

    announce(f"Connecting to Postgres at {config.target} as {config.user}...")
    return psycopg2.connect(**config.connection_kwargs())


@contextmanager
def connection(config: DatabaseConfig) -> Iterator[Any]:
    """An open connection that commits on success and rolls back on failure.

    ``psycopg2``'s own connection context manager wraps the *transaction* and
    leaves the socket open, which in a long-lived LangGraph Server process
    leaks one connection per graph run. This wrapper closes it in a ``finally``
    so that holds even on the paths that raise.

    Yields:
        The open connection.
    """
    conn = connect(config)
    try:
        yield conn
        conn.commit()
    except Exception:
        # Explicit rather than implicit: an aborted transaction left open would
        # be rolled back by the close below anyway, but only after the reason
        # has been lost from the log.
        conn.rollback()
        raise
    finally:
        conn.close()


def table_exists(cursor: Any, table_name: str = TABLE_NAME) -> bool:
    """Whether ``table_name`` is present in the ``public`` schema."""
    cursor.execute(TABLE_EXISTS_SQL, (table_name,))
    row = cursor.fetchone()
    return bool(row and row[0])


def initialize_database(config: DatabaseConfig) -> str:
    """Bring the investigations table into a known-empty state.

    Create-or-truncate rather than drop-and-recreate: truncating leaves the
    column types, the primary key and any index or grant a deployment has added
    exactly as they were, where a drop would silently discard all of them and
    replace the table with whatever this release happens to declare.

    Args:
        config: Where to connect and as whom.

    Returns:
        ``"truncated"`` if the table was already there, ``"created"`` if it was
        not — the caller reports which, since the two mean very different
        things to whoever ran the script.

    Raises:
        Exception: Any connection or statement failure, unhandled on purpose.
    """
    with connection(config) as conn, conn.cursor() as cursor:
        if table_exists(cursor):
            announce(f"Table {TABLE_NAME!r} exists; truncating it")
            cursor.execute(TRUNCATE_TABLE_SQL)
            return "truncated"

        announce(f"Table {TABLE_NAME!r} not found; creating it")
        cursor.execute(CREATE_TABLE_SQL)
        return "created"


def upsert_investigation(
    config: DatabaseConfig,
    *,
    investigation_id: str,
    application_name: str,
    confidence_score: int | None,
    analysis_mode: str,
    llm_provider: str,
    structured_report: dict[str, Any],
) -> None:
    """Store one investigation, replacing any row with the same id.

    Args:
        config: Where to connect and as whom.
        investigation_id: The primary key, supplied by the caller.
        application_name: The application the logs came from.
        confidence_score: The published 0-100 score, or ``None`` when the run
            produced none — stored as SQL ``NULL`` rather than as ``0``, which
            would read as "no confidence" instead of "not measured".
        analysis_mode: The normalized reasoning tier the run used.
        llm_provider: The normalized vendor the run used.
        structured_report: The complete report, stored as ``JSONB``.

    Raises:
        Exception: Any connection or statement failure, unhandled on purpose —
            the node owns the decision to degrade, and it needs the reason to
            put in its note.
    """
    from psycopg2.extras import Json

    with connection(config) as conn, conn.cursor() as cursor:
        cursor.execute(
            UPSERT_SQL,
            (
                investigation_id,
                application_name,
                confidence_score,
                analysis_mode,
                llm_provider,
                # ``Json`` adapts the dict to the JSONB parameter. Passing the
                # dict raw fails with "can't adapt type 'dict'"; passing
                # ``json.dumps`` output makes it a quoted JSON *string* inside
                # the column, which validates and is wrong.
                Json(structured_report),
            ),
        )


__all__ = [
    "LOG_PREFIX",
    "announce",
    "connect",
    "connection",
    "initialize_database",
    "load_env_file",
    "table_exists",
    "upsert_investigation",
]

"""Connection handling and the two operations built on it.

Everything that touches ``psycopg2`` lives here, and it is imported *lazily* —
inside the function that needs it rather than at module scope. That is the same
rule :mod:`graph_library.error_analysis.llm_factory` applies to the provider
SDKs, and it matters more here: this module is reachable from ``graph.py``
through the node registry, so a top-level import would make the driver a hard
requirement of building the graph at all. A deployment that never persists
anything would fail to start.

Two callers, two entry points:

    * :func:`initialize_database` — used by the root ``init_db.py`` to bring the
      schema up to date without touching a single stored row;
    * :func:`upsert_investigation` — used by the node to store one report.

Neither swallows an exception. Failure is the node's decision to absorb and the
script's decision to report, and a helper that returned ``False`` on error would
take the reason away from both.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NamedTuple

from .config import DatabaseConfig
from .queries import (
    COUNT_ROWS_SQL,
    CREATE_TABLE_SQL,
    INDEX_DDL,
    INDEX_EXISTS_SQL,
    TABLE_EXISTS_SQL,
    TABLE_NAME,
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
    """Load the resolved environment file, reporting which one it was.

    A thin delegate to :func:`graph_library.env_files.load_env_file`, which owns
    the selection rule for the whole project: ``ENV_FILE`` if it names a file,
    else ``.env.docker`` when a container indicator is present, else ``.env``.
    That logic used to live here, and here was the wrong home for it — this
    package persists investigations, and every other entry point had to either
    import a database module to read its configuration or grow a second,
    silently divergent copy of the rule.

    Kept as a name rather than removed because it is the function
    ``init_db.py`` imports, and because "load the project's environment file" is
    a reasonable thing to reach for from this package's public surface.

    A no-op when no file is found or ``python-dotenv`` is absent: the variables
    may perfectly well be exported by the shell, by a Compose ``environment:``
    block or by a secrets manager, none of which involve a file. Values already
    in the environment always win over a checked-in placeholder.

    Never raises. Every outcome is reported to stdout and to the logger with a
    ``[Config]`` prefix, so a run always says which file it read.
    """
    from graph_library.env_files import load_env_file as _load

    _load()


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


def index_exists(cursor: Any, index_name: str, table_name: str = TABLE_NAME) -> bool:
    """Whether ``index_name`` is present on ``table_name`` in ``public``."""
    cursor.execute(INDEX_EXISTS_SQL, (table_name, index_name))
    row = cursor.fetchone()
    return bool(row and row[0])


def row_count(cursor: Any) -> int:
    """How many investigations the table holds."""
    cursor.execute(COUNT_ROWS_SQL)
    row = cursor.fetchone()
    return int(row[0]) if row else 0


class SchemaInitResult(NamedTuple):
    """What one initialization run found, and what it had to add.

    A record of the *difference* the run made rather than a bare success flag,
    because "created the table" and "found everything already in place" are the
    two outcomes an operator wants distinguished — and because
    :attr:`preserved_rows` is what makes the non-destructive guarantee
    checkable instead of merely documented.

    Attributes:
        table_created: ``True`` when this run created the table, ``False`` when
            it was already there.
        indexes_created: Names of the indexes this run added.
        indexes_present: Names of the indexes that already existed.
        preserved_rows: Rows in the table after initialization. On a run that
            created the table this is ``0``; on any other it is the count that
            was there before, untouched.
    """

    table_created: bool
    indexes_created: tuple[str, ...]
    indexes_present: tuple[str, ...]
    preserved_rows: int

    @property
    def changed(self) -> bool:
        """Whether this run had to add anything at all."""
        return self.table_created or bool(self.indexes_created)

    @property
    def summary(self) -> str:
        """A one-line description of what happened, for a log or a CLI."""
        table = "created" if self.table_created else "already present"
        if self.indexes_created:
            indexes = f"{len(self.indexes_created)} index(es) created"
        else:
            indexes = f"{len(self.indexes_present)} index(es) already present"
        return (
            f"table {TABLE_NAME!r} {table}, {indexes}, "
            f"{self.preserved_rows} row(s) preserved"
        )


def initialize_database(config: DatabaseConfig) -> SchemaInitResult:
    """Bring the investigations schema up to date, non-destructively.

    Idempotent and safe to run on a database that holds live investigations.
    Every statement it issues is guarded — ``CREATE TABLE IF NOT EXISTS`` and
    ``CREATE INDEX IF NOT EXISTS`` — so calling this twice, or on every
    container boot, adds nothing the second time and raises nothing. **No
    existing row is read, rewritten or removed.** There is no ``TRUNCATE``, no
    ``DROP`` and no sequence reset anywhere in this package; the primary key is
    the caller's ``investigation_id``, so there is no sequence to reset in the
    first place.

    That is a deliberate reversal of what this function used to do. It
    previously truncated an existing table to hand back a clean slate, which
    made a schema check and a data wipe the same operation — so a deployment
    that ran initialization on startup, or an operator who ran it twice to be
    sure, destroyed every stored report. Preparing a clean slate is now a
    separate act, and not one this code path performs.

    The existence checks are for reporting only. The ``IF NOT EXISTS`` guards
    are what actually decide, so two processes initializing at once cannot race
    between a check and its statement.

    All the work shares one connection and one transaction, so the schema is
    either fully applied or not applied at all, and the connection is closed on
    every path by :func:`connection`.

    Args:
        config: Where to connect and as whom.

    Returns:
        A :class:`SchemaInitResult` describing what was found and what was
        added.

    Raises:
        Exception: Any connection or statement failure, unhandled on purpose —
            ``init_db.py`` turns it into an exit code and an actionable
            sentence.
    """
    announce(
        f"Verifying schema on {config.target} — non-destructive: existing rows "
        "are never modified or removed"
    )

    with connection(config) as conn, conn.cursor() as cursor:
        # -- the table ------------------------------------------------------
        announce(f"Checking for table {TABLE_NAME!r}...")
        table_was_present = table_exists(cursor)
        cursor.execute(CREATE_TABLE_SQL)
        if table_was_present:
            announce(f"Table {TABLE_NAME!r} verified; left exactly as it was")
        else:
            announce(f"Table {TABLE_NAME!r} not found; created it")

        # -- its indexes ----------------------------------------------------
        created: list[str] = []
        present: list[str] = []
        for name, statement in INDEX_DDL:
            announce(f"Checking for index {name!r}...")
            was_present = index_exists(cursor, name)
            cursor.execute(statement)
            if was_present:
                present.append(name)
                announce(f"Index {name!r} verified")
            else:
                created.append(name)
                announce(f"Index {name!r} not found; created it")

        # -- what survived --------------------------------------------------
        # Counted inside the same transaction as the DDL above, so the number
        # reported is the number the schema work actually ran against.
        preserved = row_count(cursor)

        result = SchemaInitResult(
            table_created=not table_was_present,
            indexes_created=tuple(created),
            indexes_present=tuple(present),
            preserved_rows=preserved,
        )

    announce(f"Schema initialization complete: {result.summary}")
    return result


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
    "SchemaInitResult",
    "announce",
    "connect",
    "connection",
    "index_exists",
    "initialize_database",
    "load_env_file",
    "row_count",
    "table_exists",
    "upsert_investigation",
]

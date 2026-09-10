"""Initialize the LogSherlock investigations schema.

Safe to run at any time, on any database, as many times as you like:

    python3 init_db.py

Local development reads ``.env``; a Docker Compose deployment sets the same
five ``DB_*`` variables in the service environment and needs no file. One code
path serves both, because the only difference between them is what the values
are.

What it does, in one connection and one transaction:

    * loads the resolved environment file, if there is one to load;
    * connects to PostgreSQL with the ``DB_*`` credentials;
    * **creates** the ``investigations`` table if it is absent, and leaves it
      exactly as it is if it is present;
    * **creates** each declared index if it is absent, and verifies it if it is
      present;
    * reports how many stored rows it left untouched.

**It destroys nothing.** Every statement is guarded with ``IF NOT EXISTS``,
there is no ``DROP``, no ``TRUNCATE`` and no sequence reset anywhere in the
path this script drives, and no existing row is read for modification,
rewritten or removed. Running it a second time adds nothing and raises nothing,
which is what makes it safe to wire into a container entrypoint or a deployment
step that cannot know whether the schema is already there.

That is a deliberate reversal. This script used to truncate an existing table
in order to hand back a clean slate, which made "check the schema" and "delete
every investigation" the same command — so running it twice, or running it on
startup, destroyed every stored report. Emptying the table is now something an
operator does explicitly in ``psql``, with the consequences in view, rather
than something a setup script does as a side effect of verifying a schema.

The schema, the statements and the connection handling all live in
:mod:`graph_library.write_to_db` and are shared with the node that writes to
this table. Nothing is redeclared here, so the two cannot drift apart.
"""

from __future__ import annotations

import logging
import sys

from graph_library.write_to_db import (
    TABLE_NAME,
    DatabaseConfig,
    SchemaInitResult,
    initialize_database,
    load_env_file,
)

#: Exit codes. ``2`` is separated from ``1`` because the two call for different
#: fixes: an unreachable server or a rejected credential is a deployment
#: problem, a missing driver is an install problem, and a CI step that treats
#: them alike will retry the one that cannot succeed.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_DRIVER = 2

logger = logging.getLogger(__name__)


def init_db(config: DatabaseConfig | None = None) -> SchemaInitResult:
    """Ensure the investigations schema exists, without touching any data.

    The importable form of this script, so a test, a container entrypoint or an
    application startup hook can initialize the schema without shelling out and
    without going through :func:`main`'s exit codes. Idempotent: call it once or
    call it on every boot, and a database that already holds investigations is
    left with every one of them intact.

    It deliberately does **not** load an environment file. Populating
    ``os.environ`` is an entry point's job — ``load_dotenv`` mutates the
    environment for the whole process, so a library-style call that did it would
    inject every key in the file, provider credentials included, into a caller
    that had deliberately not set them. :func:`main` loads the file; this
    function reads the environment as it stands.

    Args:
        config: Where to connect and as whom. Defaults to
            :meth:`DatabaseConfig.from_env`, which is what the CLI passes after
            the environment file has been loaded.

    Returns:
        A :class:`SchemaInitResult` describing what was already there and what
        had to be created, including the number of rows left untouched.

    Raises:
        ImportError: If ``psycopg2`` is not installed.
        Exception: Any connection or statement failure, propagated so the
            caller can decide what it means.
    """
    return initialize_database(config or DatabaseConfig.from_env())


def main() -> int:
    """Create the investigations table and its indexes if they are absent.

    Returns:
        A process exit code. Failures are reported and returned rather than
        raised, so the output ends with an actionable sentence instead of a
        traceback whose last frame is inside the driver.
    """
    # ``WARNING`` rather than ``INFO``, and that is the whole point of the line.
    # ``announce`` deliberately writes to the logger *and* to stdout, because a
    # node running under LangGraph Server needs both; here, where the root
    # logger has a console handler, letting INFO through would print every step
    # twice. Warnings and errors still surface.
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    load_env_file()
    config = DatabaseConfig.from_env()

    print(f"Target: {config.target} (user {config.user})")

    try:
        result = init_db(config)
    except ImportError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        print("Install it with: pip install psycopg2-binary", file=sys.stderr)
        return EXIT_NO_DRIVER
    except Exception as exc:  # noqa: BLE001 - reported, not propagated
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "Check that PostgreSQL is running and that DB_HOST, DB_PORT, "
            "DB_NAME, DB_USER and DB_PASSWORD are correct.",
            file=sys.stderr,
        )
        return EXIT_FAILED

    # Two sentences rather than one verb, because "created it" and "it was
    # already correct" are both successes and an operator reading a deployment
    # log needs to know which of the two happened.
    verb = "created" if result.table_created else "verified"
    print(f"\nOK: table {TABLE_NAME!r} {verb} on {config.target}")
    if result.indexes_created:
        print(f"    Indexes created:  {', '.join(result.indexes_created)}")
    if result.indexes_present:
        print(f"    Indexes verified: {', '.join(result.indexes_present)}")
    print(f"    Rows preserved:   {result.preserved_rows} (nothing was deleted)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

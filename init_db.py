"""Initialize the LogSherlock investigations database.

Run once before a session of investigations, in either deployment:

    python3 init_db.py

Local development reads ``.env``; a Docker Compose deployment sets the same
five ``DB_*`` variables in the service environment and needs no file. One code
path serves both, because the only difference between them is what the values
are.

What it does, in one connection:

    * loads ``.env`` into the environment, if there is one to load;
    * connects to PostgreSQL with the ``DB_*`` credentials;
    * **truncates** the ``investigations`` table if it already exists;
    * **creates** it if it does not.

Create-or-truncate rather than drop-and-recreate: truncating leaves the column
types, the primary key and any index or grant a deployment has added exactly as
they were, where a drop would discard all of them silently and replace the
table with whatever this release happens to declare.

**This script empties the table.** That is its purpose — it prepares a clean
slate — but it means it is not something to point at a database whose contents
matter. It reports the target and what it did on every run, so a mistake is
visible in the output rather than only in the consequences.

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


def main() -> int:
    """Create or truncate the investigations table.

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
        action = initialize_database(config)
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

    verb = "created" if action == "created" else "truncated"
    print(f"\nOK: table {TABLE_NAME!r} {verb} on {config.target}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

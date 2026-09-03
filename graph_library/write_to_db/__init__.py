"""LogSherlock write-to-db feature package.

The graph's persistence step, and the only node that leaves the process. It
stores the ``structured_report`` that ``prepare_output`` assembled in a
PostgreSQL ``investigations`` table, keyed by the caller's
``investigation_id``.

The package is laid out by concern, mirroring its sibling feature packages:

    * :mod:`graph_library.write_to_db.config` — where to connect and as whom,
      read from the environment and never from graph state,
    * :mod:`graph_library.write_to_db.queries` — every SQL statement, in one
      reviewable place,
    * :mod:`graph_library.write_to_db.db` — connection handling and the two
      operations built on it,
    * :mod:`graph_library.write_to_db.node` — the graph node that ties them
      together.

Two consumers share that surface, which is why the operations are functions in
``db`` rather than methods on the node: ``graph.py`` imports
:func:`write_to_db_node`, and the root ``init_db.py`` imports
:func:`initialize_database` and :class:`DatabaseConfig`. The schema is therefore
declared once, and the script that creates the table and the node that writes
to it cannot drift apart.

``psycopg2`` is imported lazily, inside the functions that use it. This package
is reachable from ``graph.py`` through the node registry, so a module-scope
import would make the driver a hard requirement of *building* the graph — a
deployment that never persists anything would fail to start rather than simply
never call this node.

Public surface:

    * :func:`write_to_db_node` — the graph node entry point,
    * :func:`initialize_database`, :class:`DatabaseConfig` and the helpers
      below — for ``init_db.py``, for reuse and for testing.
"""

from __future__ import annotations

from .config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_DBNAME,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_USER,
    DatabaseConfig,
)
from .db import (
    LOG_PREFIX,
    announce,
    connect,
    connection,
    initialize_database,
    load_env_file,
    table_exists,
    upsert_investigation,
)
from .node import (
    GENERATED_ID_HEX_CHARS,
    GENERATED_ID_PREFIX,
    MAX_INVESTIGATION_ID_CHARS,
    RELATIONAL_ATTRIBUTES,
    UNKNOWN_APPLICATION,
    generate_investigation_id,
    write_to_db_node,
)
from .queries import (
    CREATE_TABLE_SQL,
    TABLE_EXISTS_SQL,
    TABLE_NAME,
    TRUNCATE_TABLE_SQL,
    UPSERT_SQL,
)

__all__ = [
    "write_to_db_node",
    # config
    "DatabaseConfig",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_DBNAME",
    "DEFAULT_USER",
    "DEFAULT_CONNECT_TIMEOUT",
    # db
    "initialize_database",
    "upsert_investigation",
    "connect",
    "connection",
    "table_exists",
    "load_env_file",
    "announce",
    "LOG_PREFIX",
    # queries
    "TABLE_NAME",
    "CREATE_TABLE_SQL",
    "TABLE_EXISTS_SQL",
    "TRUNCATE_TABLE_SQL",
    "UPSERT_SQL",
    # node
    "generate_investigation_id",
    "RELATIONAL_ATTRIBUTES",
    "UNKNOWN_APPLICATION",
    "MAX_INVESTIGATION_ID_CHARS",
    "GENERATED_ID_PREFIX",
    "GENERATED_ID_HEX_CHARS",
]

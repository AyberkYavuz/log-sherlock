"""Database connection settings, read from the environment.

Credentials come from the environment and never from graph state, for the same
reason the LLM providers' keys do in
:mod:`graph_library.error_analysis.llm_factory`: a value that lives in state can
end up in a checkpoint, a LangSmith trace or a persisted report. Nothing in this
package accepts a password as an argument from a node.

The five ``DB_*`` variables are the contract with ``.env`` and with a Docker
Compose service definition alike — the only difference between the two
deployments is what the values are, which is what makes one configuration path
serve both.
"""

from __future__ import annotations

import os
from typing import Any, NamedTuple

#: Applied when the variable is absent or empty. They describe an ordinary local
#: PostgreSQL, so a developer who has one running needs no ``.env`` at all,
#: while a Compose deployment overrides every one of them.
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5432
DEFAULT_DBNAME = "postgres"
DEFAULT_USER = "postgres"

#: Seconds to wait for a connection before giving up. Deliberately short and
#: deliberately *not* unbounded: this node sits at the end of a graph run, and
#: an unreachable database must degrade in seconds rather than hang a run that
#: has already produced its whole report. Override with ``DB_CONNECT_TIMEOUT``.
DEFAULT_CONNECT_TIMEOUT = 5


def _env(name: str, default: str) -> str:
    """Read an environment variable, treating an empty value as absent.

    ``.env`` files carry empty assignments (``DB_HOST=``) as placeholders far
    more often than they carry deliberate empty strings, and ``os.getenv`` hands
    those back as ``""`` rather than falling through to the default. Every read
    in this module goes through here so that a half-filled ``.env`` behaves the
    same as a missing one.
    """
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back on anything unusable.

    A malformed port is a configuration mistake, not a reason to raise out of a
    node whose whole contract is that it degrades. The bad value is ignored and
    the failure surfaces where it is actionable — as a connection error naming
    the host and port that were actually used.
    """
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


class DatabaseConfig(NamedTuple):
    """Everything needed to reach the investigations database.

    Attributes:
        host: Server hostname. ``localhost`` locally, the service name under
            Docker Compose.
        port: Server port.
        dbname: The database holding the ``investigations`` table.
        user: Role to authenticate as.
        password: The role's password. Empty is legitimate — a local
            trust-authenticated PostgreSQL needs none — and is omitted from the
            connection parameters entirely rather than sent as ``""``.
        connect_timeout: Seconds to wait for the connection.
    """

    host: str
    port: int
    dbname: str
    user: str
    password: str
    connect_timeout: int

    @classmethod
    def from_env(cls) -> DatabaseConfig:
        """Build a config from the ``DB_*`` environment variables.

        Reads the environment as it stands; loading ``.env`` into it is the
        caller's job, so that a process which already exports real credentials
        is never overridden by a checked-in placeholder file. See
        :func:`graph_library.write_to_db.db.load_env_file`.

        Returns:
            A config with defaults applied for every variable that is absent or
            empty. Never raises.
        """
        return cls(
            host=_env("DB_HOST", DEFAULT_HOST),
            port=_env_int("DB_PORT", DEFAULT_PORT),
            dbname=_env("DB_NAME", DEFAULT_DBNAME),
            user=_env("DB_USER", DEFAULT_USER),
            password=os.getenv("DB_PASSWORD") or "",
            connect_timeout=_env_int("DB_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT),
        )

    def connection_kwargs(self) -> dict[str, Any]:
        """The keyword arguments for ``psycopg2.connect``.

        An empty password is omitted rather than passed as ``""``: the two are
        not the same to libpq, and sending a blank one to a trust-authenticated
        server is the difference between connecting and being rejected.
        """
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "connect_timeout": self.connect_timeout,
        }
        if self.password:
            kwargs["password"] = self.password
        return kwargs

    @property
    def target(self) -> str:
        """A ``host:port/dbname`` label safe to write to a log or a report.

        The password is not merely omitted here, it has no representation: this
        string is what every log line and every investigation note about the
        database is built from, and a credential must not be one substitution
        away from any of them.
        """
        return f"{self.host}:{self.port}/{self.dbname}"


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_DBNAME",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_USER",
    "DatabaseConfig",
]

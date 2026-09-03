"""Runtime settings for the LogSherlock HTTP API.

Two configurations meet here and are deliberately kept apart:

    * :class:`ApiSettings` — how the *server* runs: bind address, CORS origins,
      keep-alive budget, log level. Owned by this package.
    * :class:`~graph_library.write_to_db.config.DatabaseConfig` — where the
      *database* is and as whom to connect. Owned by
      :mod:`graph_library.write_to_db` and imported rather than redeclared, so
      the API, the ``write_to_db`` node and ``init_db.py`` cannot drift onto
      three different databases.

Both read the environment as it stands and never load ``.env`` themselves. That
is the rule the whole project holds — ``load_dotenv`` mutates ``os.environ`` for
the entire process, so a library that calls it injects every key in the file
into a process that deliberately did not set them. Populating the environment
belongs to the entry point, which here is ``backend.py``.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field

from graph_library.write_to_db import DatabaseConfig

#: Loopback rather than ``0.0.0.0``: this server holds a database credential and
#: an LLM key, and a development default that binds every interface is one
#: misconfigured firewall away from being public. Override with ``API_HOST``.
DEFAULT_HOST = "127.0.0.1"

#: Chosen to stay clear of both ports the local-LLM path already uses: 8000 is
#: ``llm_factory.DEFAULT_LOCAL_BASE_URL``, and 8080 is where ``GRAPH_README``
#: has the developer start ``tests/mock_local_llm.py``. Running the API
#: alongside a mock provider is the normal offline setup, so a default that
#: fights either of them for a socket would be a default that fails on its
#: first use.
DEFAULT_PORT = 8010

#: The React development servers this API is built for: Create React App on
#: 3000 and Vite on 5173, each on both spellings of loopback because a browser
#: sends the Origin header exactly as the address bar spells it and
#: ``CORSMiddleware`` matches it as an opaque string.
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

#: Seconds an idle connection is held open between requests. Raised well above
#: uvicorn's default 5 because ``POST /api/investigate`` runs a whole LangGraph
#: pipeline — eight nodes, up to four LLM calls and an optional web search — and
#: a browser that had its connection closed mid-run would report a network error
#: for an investigation that actually completed.
#:
#: Note this is the *keep-alive* budget, not a request deadline: uvicorn imposes
#: no ceiling on how long a handler may run, which is exactly what a
#: minutes-long graph invocation needs.
DEFAULT_KEEP_ALIVE_TIMEOUT = 75

#: How long the graph may run before the API gives up on it. Generous because
#: the payload size is the caller's choice and a 4 MB corpus legitimately takes
#: minutes; bounded because a wedged provider call must not pin a worker
#: forever. Override with ``API_GRAPH_TIMEOUT`` (``0`` disables the deadline).
DEFAULT_GRAPH_TIMEOUT = 900.0


def _env(name: str, default: str) -> str:
    """Read an environment variable, treating an empty value as absent.

    The same helper :mod:`graph_library.write_to_db.config` uses, and for the
    same reason: a ``.env`` carries ``API_PORT=`` as a placeholder far more
    often than it carries a deliberate empty string, and ``os.getenv`` hands
    those back as ``""`` rather than falling through to the default.
    """
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    """Read an integer variable, ignoring anything unusable.

    A malformed port is a configuration mistake, not a reason to refuse to
    start: the server comes up on the default and the operator sees the address
    it actually bound in the startup line.
    """
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float variable, ignoring anything unusable."""
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_origins(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Read a comma-separated origin list, falling back to ``default``."""
    raw = _env(name, "")
    if not raw:
        return default
    origins = tuple(origin.strip() for origin in raw.split(",") if origin.strip())
    return origins or default


class ApiSettings(BaseModel):
    """How the HTTP server itself should run.

    Attributes:
        host: Address to bind.
        port: Port to bind.
        cors_origins: Browser origins allowed to call this API.
        keep_alive_timeout: Seconds an idle connection is held open.
        graph_timeout: Seconds a single graph invocation may run before the
            request is failed. ``0`` or less disables the deadline.
        log_level: uvicorn's log level for this process.
        reload: Whether to run uvicorn's auto-reloader. Development only — it
            re-imports the application on every file change, which would
            discard the compiled graph mid-investigation.
    """

    # Frozen because these values are read on every request through a cached
    # dependency: a settings object that could be mutated at runtime would make
    # two concurrent requests disagree about their own configuration.
    model_config = ConfigDict(frozen=True)

    host: str = DEFAULT_HOST
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    keep_alive_timeout: int = Field(default=DEFAULT_KEEP_ALIVE_TIMEOUT, ge=1)
    graph_timeout: float = Field(default=DEFAULT_GRAPH_TIMEOUT, ge=0.0)
    log_level: str = "info"
    reload: bool = False

    @classmethod
    def from_env(cls) -> ApiSettings:
        """Build settings from the ``API_*`` environment variables.

        Every variable is optional and every default is a working local
        development value, so the API starts with no configuration at all.

        Returns:
            A populated, frozen settings object. Never raises — an unusable
            value falls back to its default rather than refusing to boot.
        """
        return cls(
            host=_env("API_HOST", DEFAULT_HOST),
            port=_env_int("API_PORT", DEFAULT_PORT),
            cors_origins=_env_origins("API_CORS_ORIGINS", DEFAULT_CORS_ORIGINS),
            keep_alive_timeout=_env_int(
                "API_KEEP_ALIVE_TIMEOUT", DEFAULT_KEEP_ALIVE_TIMEOUT
            ),
            graph_timeout=_env_float("API_GRAPH_TIMEOUT", DEFAULT_GRAPH_TIMEOUT),
            log_level=_env("API_LOG_LEVEL", "info").lower(),
            reload=_env("API_RELOAD", "").lower() in {"1", "true", "yes", "on"},
        )

    @property
    def bind_target(self) -> str:
        """A ``host:port`` label for a log line or a startup banner."""
        return f"{self.host}:{self.port}"


def database_config_from_env() -> DatabaseConfig:
    """The database settings this API should use.

    A one-line indirection rather than a direct call at every site, so the whole
    backend has exactly one place where "which database?" is answered — and so a
    test can monkeypatch that one place. The answer itself comes from
    :mod:`graph_library.write_to_db`, which is what the persistence node and
    ``init_db.py`` also read.
    """
    return DatabaseConfig.from_env()


__all__ = [
    "DEFAULT_CORS_ORIGINS",
    "DEFAULT_GRAPH_TIMEOUT",
    "DEFAULT_HOST",
    "DEFAULT_KEEP_ALIVE_TIMEOUT",
    "DEFAULT_PORT",
    "ApiSettings",
    "database_config_from_env",
]

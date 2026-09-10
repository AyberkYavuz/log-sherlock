"""The application factory.

:func:`create_app` is the one place the whole backend is assembled: settings,
the service factory, CORS, the exception handlers and the routers. It is a
*function* rather than a module-level ``app = FastAPI()`` for two reasons that
both matter here:

    * a test builds an application wired to stubs by passing one argument,
      rather than by importing a global and patching around it;
    * nothing is constructed at import time, so ``import backend`` does not read
      the environment, resolve a database or compile the graph.

The lifespan hook does two things. It logs what the process is actually wired
to — the bind address, the allowed origins and, in production wiring, the
database that is about to be read; that last line is the single most useful
thing in the log when a deployment turns out to be serving an empty list from
the wrong server. And it verifies the investigations schema before the first
request is served, so a fresh database does not have to be prepared by hand.

**Schema verification runs only under production wiring**, and that is a
correctness requirement rather than an optimization: an application built over
test doubles has no database to verify, and a startup hook that reached for one
anyway would make every offline test depend on a running PostgreSQL.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from anyio import to_thread
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from graph_library.env_files import loaded_env_file
from graph_library.write_to_db import DatabaseConfig, initialize_database

from .config import ApiSettings
from .dependencies import SERVICE_FACTORY_ATTRIBUTE
from .errors import register_exception_handlers
from .factories import DefaultServiceFactory, PostgresRepositoryFactory, ServiceFactory
from .routes import health_router, investigations_router

logger = logging.getLogger(__name__)

#: Prefix on every line the startup and shutdown hook emits, so one ``grep``
#: separates "what happened while the process was booting" from everything the
#: request path logs afterwards. Distinct from ``write_to_db``'s
#: ``[LogSherlock DB]``, which the schema work itself uses — the two appear
#: interleaved during startup, and telling "the hook decided to verify" from
#: "the database answered" is the whole reason they differ.
LIFESPAN_LOG_PREFIX = "[FastAPI Lifespan]"

#: Mounted under ``/api`` so the whole surface is reachable behind one proxy
#: rule and cannot collide with a static route a frontend server owns.
API_PREFIX = "/api"

TITLE = "LogSherlock API"
DESCRIPTION = (
    "HTTP surface for the LogSherlock multi-agent log analysis graph: run an "
    "investigation through the LangGraph pipeline, then list, read and delete "
    "the reports it stored."
)
VERSION = "0.1.0"


def _database_config(factory: ServiceFactory) -> DatabaseConfig | None:
    """The database this application reads from, or ``None`` if it has none.

    ``None`` is the test wiring: a :class:`~backend.factories.StubServiceFactory`
    (or any other substitute) has no PostgreSQL behind it, so there is nothing
    to name in a log line and nothing to verify at startup.

    The config is taken from the repository factory rather than re-read from the
    environment, which is what keeps the schema that gets verified and the
    database that gets queried from being two different servers.
    """
    if isinstance(factory, DefaultServiceFactory) and isinstance(
        factory.repository_factory, PostgresRepositoryFactory
    ):
        return factory.repository_factory.config
    return None


async def _verify_schema(config: DatabaseConfig) -> None:
    """Create the investigations table and its indexes if they are absent.

    Idempotent and non-destructive — see
    :func:`graph_library.write_to_db.initialize_database`. Every statement is
    ``CREATE ... IF NOT EXISTS``, so a database already holding investigations
    keeps every one of them; that property is what makes this safe to run on
    every boot rather than once by hand.

    Run in a worker thread. ``initialize_database`` is synchronous ``psycopg2``
    I/O that blocks for up to ``DB_CONNECT_TIMEOUT`` seconds against an
    unreachable server, and blocking the event loop through startup would also
    block the signal handling that is supposed to let an operator interrupt it.

    Args:
        config: Where to connect and as whom.

    Raises:
        Exception: Whatever the driver raised, re-raised after logging so
            startup fails loudly. See the comment at the call site for why
            failing is the right answer here.
    """
    logger.info(
        "%s Starting automated database schema verification on %s...",
        LIFESPAN_LOG_PREFIX,
        config.target,
    )
    try:
        result = await to_thread.run_sync(initialize_database, config)
    except Exception:
        # ``exception`` rather than ``error``: it attaches the full traceback,
        # which is the only thing that distinguishes a refused connection from
        # a rejected credential from a missing database in a container log
        # nobody can attach a debugger to.
        logger.exception(
            "%s Database schema verification FAILED on %s. The API will not "
            "start. Check that PostgreSQL is running and that DB_HOST, DB_PORT, "
            "DB_NAME, DB_USER and DB_PASSWORD are correct.",
            LIFESPAN_LOG_PREFIX,
            config.target,
        )
        raise
    logger.info(
        "%s Database schema verification complete on %s: %s",
        LIFESPAN_LOG_PREFIX,
        config.target,
        result.summary,
    )


def _build_lifespan(settings: ApiSettings, factory: ServiceFactory):
    """Create the startup/shutdown hook for one application.

    Closed over the settings and factory rather than reading them off
    ``app.state``, so the hook cannot observe a half-built application.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "%s %s starting on %s (CORS origins: %s, graph timeout: %s)",
            TITLE,
            VERSION,
            settings.bind_target,
            ", ".join(settings.cors_origins),
            f"{settings.graph_timeout:.0f}s" if settings.graph_timeout else "none",
        )

        # Which file these settings came from, reported through the *logger*
        # rather than stdout. The entry point already printed it, but that print
        # happens before logging is configured, so it never reaches a log file
        # or an aggregator. This line is the one a deployment actually captures,
        # and "the wrong env file" is the first thing to rule out when a running
        # service is configured in a way nobody expects.
        #
        # Read rather than resolved: this reports what was loaded, so it cannot
        # disagree with the process's real configuration. ``None`` means no
        # entry point loaded a file — the normal case under a test client, or
        # when an orchestrator supplies the environment directly.
        environment = loaded_env_file()
        if environment is None:
            logger.info(
                "Environment file: none loaded in this process (variables read "
                "as supplied)"
            )
        else:
            logger.info(
                "Environment file: %s (%d keys, %s, loaded=%s)",
                environment.name or "(none found)",
                environment.key_count,
                environment.selected_by,
                environment.loaded,
            )
        # Production wiring only — a test factory has no database. The target
        # carries no credential (see ``DatabaseConfig.target``), so it is safe
        # to log.
        database = _database_config(factory)
        if database is None:
            logger.info(
                "%s No PostgreSQL wiring in this application; schema "
                "verification skipped",
                LIFESPAN_LOG_PREFIX,
            )
        else:
            logger.info("Investigations database: %s", database.target)
            # Deliberately *not* wrapped in a ``try`` that swallows. The three
            # storage endpoints cannot work without this table, and a process
            # that boots anyway would answer every one of them with a 503 while
            # reporting itself healthy to a load balancer — a failure that
            # looks like a database outage from the outside and takes an
            # operator to the wrong system. Failing at boot puts the reason in
            # the startup log, where the person deploying is already looking,
            # and gives uvicorn a non-zero exit code for an orchestrator to
            # act on.
            #
            # The cost is real and worth naming: an API that would previously
            # have started and served ``/api/health`` and ``POST
            # /api/investigate`` against an unreachable database now refuses to
            # start at all.
            await _verify_schema(database)

        yield

        logger.info("%s %s shutting down", LIFESPAN_LOG_PREFIX, TITLE)

    return lifespan


def create_app(
    settings: ApiSettings | None = None,
    service_factory: ServiceFactory | None = None,
) -> FastAPI:
    """Build a fully wired FastAPI application.

    Args:
        settings: Server settings. Read from the environment when omitted.
        service_factory: The single seam every dependency hangs off. Defaults to
            :class:`~backend.factories.DefaultServiceFactory` — PostgreSQL
            storage and the real compiled graph. A test passes its own and
            replaces the entire object graph below it.

    Returns:
        The application, ready for uvicorn or
        :class:`~fastapi.testclient.TestClient`.

    Nothing here touches the database. Under production wiring the
    investigations schema is verified by the lifespan hook when the application
    *starts*, not when it is built, so constructing one still reads no
    environment, opens no socket and compiles no graph. A
    :class:`~fastapi.testclient.TestClient` used as a context manager runs that
    hook; one used bare does not.
    """
    settings = settings or ApiSettings.from_env()
    service_factory = service_factory or DefaultServiceFactory(settings=settings)

    app = FastAPI(
        title=TITLE,
        description=DESCRIPTION,
        version=VERSION,
        lifespan=_build_lifespan(settings, service_factory),
    )

    # The root of the dependency chain — see :mod:`backend.dependencies` for why
    # this lives on ``app.state`` rather than in a module-level global.
    setattr(app.state, SERVICE_FACTORY_ATTRIBUTE, service_factory)
    app.state.settings = settings

    # CORS before anything else. A browser sends its preflight ``OPTIONS`` to
    # the same path as the real request, and middleware added later would sit
    # inside this one and never see it.
    #
    # ``allow_origins`` is an explicit list rather than ``["*"]`` because
    # ``allow_credentials=True`` and a wildcard are mutually exclusive per the
    # CORS specification: the browser rejects the combination outright, so a
    # wildcard here would break exactly the cookie-bearing requests it looks
    # like it permits.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        # Lets a browser cache the preflight for ten minutes instead of sending
        # one before every POST in a session.
        max_age=600,
    )

    register_exception_handlers(app)

    app.include_router(health_router, prefix=API_PREFIX)
    app.include_router(investigations_router, prefix=API_PREFIX)

    return app


__all__ = [
    "API_PREFIX",
    "DESCRIPTION",
    "LIFESPAN_LOG_PREFIX",
    "TITLE",
    "VERSION",
    "create_app",
]

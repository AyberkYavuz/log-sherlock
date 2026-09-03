"""The application factory.

:func:`create_app` is the one place the whole backend is assembled: settings,
the service factory, CORS, the exception handlers and the routers. It is a
*function* rather than a module-level ``app = FastAPI()`` for two reasons that
both matter here:

    * a test builds an application wired to stubs by passing one argument,
      rather than by importing a global and patching around it;
    * nothing is constructed at import time, so ``import backend`` does not read
      the environment, resolve a database or compile the graph.

The lifespan hook logs what the process is actually wired to — the bind address,
the allowed origins and, in production wiring, the database that is about to be
read. That last line is the single most useful thing in the log when a
deployment turns out to be serving an empty list from the wrong server.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import ApiSettings
from .dependencies import SERVICE_FACTORY_ATTRIBUTE
from .errors import register_exception_handlers
from .factories import DefaultServiceFactory, PostgresRepositoryFactory, ServiceFactory
from .routes import health_router, investigations_router

logger = logging.getLogger(__name__)

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
        if isinstance(factory, DefaultServiceFactory) and isinstance(
            factory.repository_factory, PostgresRepositoryFactory
        ):
            # Production wiring only — a test factory has no database to name.
            # The target carries no credential (see ``DatabaseConfig.target``),
            # so it is safe to log.
            logger.info(
                "Investigations database: %s",
                factory.repository_factory.config.target,
            )

        yield

        logger.info("%s shutting down", TITLE)

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


__all__ = ["API_PREFIX", "DESCRIPTION", "TITLE", "VERSION", "create_app"]

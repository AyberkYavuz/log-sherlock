"""The dependency-injection wiring between FastAPI and the service layer.

Route handlers declare what they need as a parameter and FastAPI resolves it
through ``Depends``. Nothing in a handler constructs a service, reads the
environment or opens a connection, which is what keeps a route function three
lines long and testable by substitution rather than by patching.

The chain has one root. :func:`get_service_factory` reads the
:class:`~backend.factories.ServiceFactory` that
:func:`backend.app.create_app` stored on ``app.state``; everything else hangs
off it. Overriding that single dependency in a test replaces the graph, the
repository and both services at once.

``app.state`` rather than a module-level global, deliberately: a global would be
shared by every application object in the process, so two ``TestClient``
instances in one test session — one with a stub graph, one with a stub that
raises — would silently share the last one wired.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request

from .config import ApiSettings
from .factories import ServiceFactory
from .services import GraphRunnerService, InvestigationService

#: The attribute :func:`backend.app.create_app` stores the factory under.
SERVICE_FACTORY_ATTRIBUTE = "service_factory"


@lru_cache(maxsize=1)
def get_settings() -> ApiSettings:
    """The process-wide server settings.

    Cached because these are read per request and the environment does not
    change under a running server. A test that needs different settings calls
    ``get_settings.cache_clear()`` or overrides this dependency.
    """
    return ApiSettings.from_env()


def get_service_factory(request: Request) -> ServiceFactory:
    """The factory this application was wired with.

    Args:
        request: The live request, used only to reach ``app.state``.

    Returns:
        The application's :class:`~backend.factories.ServiceFactory`.

    Raises:
        RuntimeError: If the application was built without one. Only reachable
            when a ``FastAPI`` instance is assembled by hand rather than through
            :func:`backend.app.create_app`, and raised rather than defaulted
            because silently constructing a production factory here would have a
            test quietly talking to the real database.
    """
    factory = getattr(request.app.state, SERVICE_FACTORY_ATTRIBUTE, None)
    if factory is None:  # pragma: no cover - guards a wiring mistake
        raise RuntimeError(
            "No ServiceFactory on app.state. Build the application with "
            "backend.app.create_app()."
        )
    return factory


def get_investigation_service(
    factory: Annotated[ServiceFactory, Depends(get_service_factory)],
) -> InvestigationService:
    """The service backing the list, detail and delete endpoints."""
    return factory.create_investigation_service()


def get_graph_runner_service(
    factory: Annotated[ServiceFactory, Depends(get_service_factory)],
) -> GraphRunnerService:
    """The service backing ``POST /api/investigate``."""
    return factory.create_graph_runner_service()


#: Named aliases for the annotations above, so a route signature reads as the
#: contract it is (``service: InvestigationServiceDep``) rather than as a nested
#: generic repeated at four call sites.
SettingsDep = Annotated[ApiSettings, Depends(get_settings)]
ServiceFactoryDep = Annotated[ServiceFactory, Depends(get_service_factory)]
InvestigationServiceDep = Annotated[
    InvestigationService, Depends(get_investigation_service)
]
GraphRunnerServiceDep = Annotated[
    GraphRunnerService, Depends(get_graph_runner_service)
]

__all__ = [
    "SERVICE_FACTORY_ATTRIBUTE",
    "GraphRunnerServiceDep",
    "InvestigationServiceDep",
    "ServiceFactoryDep",
    "SettingsDep",
    "get_graph_runner_service",
    "get_investigation_service",
    "get_service_factory",
    "get_settings",
]

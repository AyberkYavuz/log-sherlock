"""Factories — the one place that decides which implementation is in play.

Every abstract interface in this backend has exactly one production
implementation today. The factories exist anyway, and they earn their keep for a
reason that has nothing to do with swapping vendors: they are what keeps
*construction* out of the request path. A route handler that wrote
``PostgresInvestigationRepository(DatabaseConfig.from_env())`` would be a route
handler that reads the environment on every request and that no test can point
somewhere else.

Three factories, at three levels:

    * :class:`GraphFactory` — compiles the LangGraph pipeline, once, lazily.
    * :class:`RepositoryFactory` — builds data access objects.
    * :class:`ServiceFactory` — builds services out of the two above.

:class:`DefaultServiceFactory` is what the application holds, and a test
substitutes at that single seam.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import override

from graph_library.write_to_db import DatabaseConfig

from .config import ApiSettings, database_config_from_env
from .persistence import InvestigationRepository, PostgresInvestigationRepository
from .services import (
    CompiledGraph,
    DefaultInvestigationService,
    GraphRunnerService,
    InvestigationService,
    LangGraphRunnerService,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


class GraphFactory(ABC):
    """Supplies the compiled LogSherlock graph."""

    @abstractmethod
    def get_graph(self) -> CompiledGraph:
        """Return a compiled graph, ready to invoke.

        Returns:
            The graph. Implementations are expected to be cheap on the second
            call — this runs on the request path.
        """


class CompiledGraphFactory(GraphFactory):
    """Compiles ``graph.compile_graph()`` once and hands out the result.

    Compilation walks the whole node registry and is therefore not free, but
    more importantly it is *pure*: the compiled graph holds no per-run state
    (``compile_graph`` deliberately attaches no checkpointer, because
    LogSherlock treats each investigation as a stateless request), so one
    instance safely serves every concurrent invocation.

    ``graph`` is imported inside :meth:`get_graph` rather than at module scope.
    That import pulls in every feature package and their dependencies — pandas
    among them — and doing it eagerly would make ``import backend`` pay for the
    engine even when the caller only wanted the schemas.
    """

    def __init__(self) -> None:
        self._graph: CompiledGraph | None = None
        # Two requests can arrive before the first has finished compiling. The
        # lock makes the second wait rather than compile a second graph and
        # discard one — wasteful rather than incorrect, but the wasted work is
        # the whole node stack.
        self._lock = threading.Lock()

    @override
    def get_graph(self) -> CompiledGraph:
        if self._graph is None:
            with self._lock:
                if self._graph is None:  # re-checked: another thread may have won
                    logger.info("Compiling the LogSherlock graph")
                    from graph import compile_graph

                    self._graph = compile_graph()
                    logger.info("Graph compiled and cached for this process")
        return self._graph


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


class RepositoryFactory(ABC):
    """Builds the data access objects the services depend on."""

    @abstractmethod
    def create_investigation_repository(self) -> InvestigationRepository:
        """Return a repository over stored investigations."""


class PostgresRepositoryFactory(RepositoryFactory):
    """Builds repositories over PostgreSQL.

    Resolves the database configuration once, at construction, rather than per
    repository: a server whose ``DB_HOST`` changed underneath it mid-run would
    otherwise serve two requests from two databases and report neither.
    """

    def __init__(self, config: DatabaseConfig | None = None) -> None:
        """Fix the database this factory builds against.

        Args:
            config: Where to connect and as whom. Read from the environment
                when omitted.
        """
        self._config = config or database_config_from_env()
        logger.info("Repositories will read from %s", self._config.target)

    @property
    def config(self) -> DatabaseConfig:
        """The resolved database settings — useful in a startup log line."""
        return self._config

    @override
    def create_investigation_repository(self) -> InvestigationRepository:
        return PostgresInvestigationRepository(self._config)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


class ServiceFactory(ABC):
    """Builds the services the route handlers depend on.

    The single seam the application is wired through: :func:`backend.app.create_app`
    takes one of these, and a test that supplies its own replaces every
    dependency below it in one line.
    """

    @abstractmethod
    def create_investigation_service(self) -> InvestigationService:
        """Return the service backing the three storage endpoints."""

    @abstractmethod
    def create_graph_runner_service(self) -> GraphRunnerService:
        """Return the service backing ``POST /api/investigate``."""


class DefaultServiceFactory(ServiceFactory):
    """The production wiring: Postgres storage and the compiled graph.

    Services are built once and reused, not rebuilt per request. Both are
    stateless — one holds a repository, the other a graph factory — so sharing
    them is safe, and rebuilding them would mean re-reading configuration on
    every call.
    """

    def __init__(
        self,
        *,
        settings: ApiSettings | None = None,
        repository_factory: RepositoryFactory | None = None,
        graph_factory: GraphFactory | None = None,
    ) -> None:
        """Assemble the object graph.

        Args:
            settings: Server settings, read from the environment when omitted.
                Only ``graph_timeout`` is consumed here.
            repository_factory: Where investigations are read from. Defaults to
                PostgreSQL.
            graph_factory: Where the compiled pipeline comes from. Defaults to
                compiling ``graph.compile_graph()`` on first use.
        """
        self._settings = settings or ApiSettings.from_env()
        self._repository_factory = repository_factory or PostgresRepositoryFactory()
        self._graph_factory = graph_factory or CompiledGraphFactory()

        self._investigation_service: InvestigationService = (
            DefaultInvestigationService(
                self._repository_factory.create_investigation_repository()
            )
        )
        self._graph_runner_service: GraphRunnerService = LangGraphRunnerService(
            self._graph_factory, timeout=self._settings.graph_timeout
        )

    @property
    def settings(self) -> ApiSettings:
        """The settings this factory was built with."""
        return self._settings

    @property
    def repository_factory(self) -> RepositoryFactory:
        """The repository factory in play — read by the startup log line."""
        return self._repository_factory

    @override
    def create_investigation_service(self) -> InvestigationService:
        return self._investigation_service

    @override
    def create_graph_runner_service(self) -> GraphRunnerService:
        return self._graph_runner_service


__all__ = [
    "CompiledGraphFactory",
    "DefaultServiceFactory",
    "GraphFactory",
    "PostgresRepositoryFactory",
    "RepositoryFactory",
    "ServiceFactory",
]

"""LogSherlock HTTP API — the layer between a React client and the graph.

The engine is not touched. ``graph.py`` is imported and invoked;
:mod:`graph_library.models` supplies the report shape;
:mod:`graph_library.write_to_db` supplies the table name and the connection
settings. This package adds no analysis and redefines no model — it validates
requests, runs the pipeline, reads what the pipeline stored, and reports
failures as status codes.

Layout, by concern, mirroring the feature packages under ``graph_library/``:

    * :mod:`backend.config` — server settings, and the one place the database
      settings are resolved,
    * :mod:`backend.schemas` — the Pydantic v2 request/response contract,
    * :mod:`backend.errors` — the error taxonomy and its JSON handlers,
    * :mod:`backend.persistence` — SQL, connections and the
      :class:`~backend.persistence.InvestigationRepository` interface,
    * :mod:`backend.services` — the service interfaces and their
      implementations,
    * :mod:`backend.factories` — the factories that construct all of the above,
    * :mod:`backend.dependencies` — the FastAPI ``Depends`` wiring,
    * :mod:`backend.routes` — the five endpoints,
    * :mod:`backend.app` — :func:`~backend.app.create_app`.

The dependency arrow runs one way through that list: routes know about services,
services know about repositories, and nothing below knows about FastAPI.

Run it with ``python3 backend.py`` from the repository root. That script and
this package share a name, which Python resolves in the package's favour — the
script is only ever executed, never imported, so the two never collide.
"""

from __future__ import annotations

from .app import API_PREFIX, create_app
from .config import ApiSettings, database_config_from_env
from .errors import (
    BackendError,
    GraphExecutionError,
    GraphTimeoutError,
    InvestigationNotFoundError,
    RepositoryError,
)
from .factories import (
    CompiledGraphFactory,
    DefaultServiceFactory,
    GraphFactory,
    PostgresRepositoryFactory,
    RepositoryFactory,
    ServiceFactory,
)
from .persistence import (
    InvestigationPage,
    InvestigationRepository,
    PostgresInvestigationRepository,
    StoredInvestigation,
)
from .schemas import (
    DeleteInvestigationResponse,
    ErrorResponse,
    HealthResponse,
    InvestigateRequest,
    InvestigateResponse,
    InvestigationDetailRequest,
    InvestigationDetailResponse,
    InvestigationMetadataItem,
    PaginatedInvestigationsResponse,
    PaginationParams,
)
from .services import (
    DefaultInvestigationService,
    GraphRunnerService,
    InvestigationService,
    LangGraphRunnerService,
    generate_investigation_id,
)

__version__ = "0.1.0"

__all__ = [
    "API_PREFIX",
    "ApiSettings",
    "BackendError",
    "CompiledGraphFactory",
    "DefaultInvestigationService",
    "DefaultServiceFactory",
    "DeleteInvestigationResponse",
    "ErrorResponse",
    "GraphExecutionError",
    "GraphFactory",
    "GraphRunnerService",
    "GraphTimeoutError",
    "HealthResponse",
    "InvestigateRequest",
    "InvestigateResponse",
    "InvestigationDetailRequest",
    "InvestigationDetailResponse",
    "InvestigationMetadataItem",
    "InvestigationNotFoundError",
    "InvestigationPage",
    "InvestigationRepository",
    "InvestigationService",
    "LangGraphRunnerService",
    "PaginatedInvestigationsResponse",
    "PaginationParams",
    "PostgresInvestigationRepository",
    "PostgresRepositoryFactory",
    "RepositoryError",
    "RepositoryFactory",
    "ServiceFactory",
    "__version__",
    "create_app",
    "database_config_from_env",
    "generate_investigation_id",
]

"""The API's service layer — the application logic between routes and storage.

Two services, each an abstract interface plus one implementation:

    * :mod:`backend.services.investigations` — reading and deleting stored
      investigations,
    * :mod:`backend.services.graph_runner` — running the LangGraph pipeline.

Neither imports FastAPI. A service raises a
:class:`~backend.errors.BackendError` and returns a Pydantic response model; the
route layer is what turns those into status codes. That boundary is what makes
the same service callable from a script or a scheduled job without a request in
flight.
"""

from __future__ import annotations

from .graph_runner import (
    GENERATED_ID_CHARS,
    GENERATED_ID_PREFIX,
    CompiledGraph,
    GraphFactoryProtocol,
    GraphRunnerService,
    LangGraphRunnerService,
    generate_investigation_id,
)
from .investigations import DefaultInvestigationService, InvestigationService

__all__ = [
    "GENERATED_ID_CHARS",
    "GENERATED_ID_PREFIX",
    "CompiledGraph",
    "DefaultInvestigationService",
    "GraphFactoryProtocol",
    "GraphRunnerService",
    "InvestigationService",
    "LangGraphRunnerService",
    "generate_investigation_id",
]

"""The API's HTTP surface, grouped by concern.

    * :mod:`backend.routes.health` — the liveness probe,
    * :mod:`backend.routes.investigations` — the four endpoints that run and
      read investigations.

Both are :class:`~fastapi.APIRouter` s with no prefix of their own;
:func:`backend.app.create_app` mounts them under ``/api``, so the prefix is
declared once and cannot drift between two files.
"""

from __future__ import annotations

from .health import router as health_router
from .investigations import router as investigations_router

__all__ = ["health_router", "investigations_router"]

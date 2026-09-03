"""``GET /api/health`` — is the process up and serving?

Deliberately the only endpoint that touches nothing. It does not query the
database and does not compile the graph, because a health check that fails when
a dependency is down takes the whole API out of a load balancer over a
dependency three of its five endpoints do not need — and because a check that
opens a connection is a check that can hang.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from ..schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Liveness probe",
)
async def health() -> HealthResponse:
    """Report that the backend is running.

    Returns:
        A fixed ``{"status": "ok", "message": "Backend is running"}``.
    """
    return HealthResponse()


__all__ = ["router"]

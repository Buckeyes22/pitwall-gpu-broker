"""FastAPI route handler for POST /v1/admin/audit-capability/{name}.

The handler delegates to :func:`pitwall.audit.capability.audit_capability`,
which runs the eight required pre-spend checks and returns a
:class:`pitwall.audit.capability.CapabilityAuditResult`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from pitwall.audit.capability import audit_capability
from pitwall.db.repository import CapabilityRepository, ProviderRepository

router = APIRouter()


def _capability_repo(request: Request) -> CapabilityRepository:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return CapabilityRepository(pool)


def _provider_repo(request: Request) -> ProviderRepository:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return ProviderRepository(pool)


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return pool


@router.post("/v1/admin/audit-capability/{name}")
async def audit_capability_endpoint(
    name: str,
    request: Request,
    capability_repo: CapabilityRepository = Depends(_capability_repo),
    provider_repo: ProviderRepository = Depends(_provider_repo),
    pool: Any = Depends(_pool),
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if request.query_params:
        payload = dict(request.query_params)

    result = await audit_capability(
        name,
        capability_repo=capability_repo,
        provider_repo=provider_repo,
        pool=pool,
        payload=payload or None,
    )
    return result.model_dump()


__all__ = ["router"]

"""FastAPI route handlers for the Provider CRUD surface.

Admin routes (POST/PATCH/enable/disable/hibernate) are mounted under
``/v1/admin/providers``. Discovery routes (GET list/health) are mounted
under ``/v1/providers``.

Each handler delegates to :class:`pitwall.db.repository.ProviderRepository`
and raises mapped API exceptions that FastAPI exception handlers translate to
the correct HTTP status codes.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from pitwall.api.exceptions import (
    ProviderCapabilityMissing,
    ProviderConflict,
    ProviderNotFound,
)
from pitwall.api.provider_schemas import (
    ProviderCreate,
    ProviderHealthResponse,
    ProviderHibernateResponse,
    ProviderPatch,
    ProviderResponse,
    validate_provider_registration_config,
)
from pitwall.api.schemas.params import OptionalStrQuery, PathId
from pitwall.core.enums import ProviderType
from pitwall.core.ids import ulid_new
from pitwall.core.models import Provider
from pitwall.db.repository import CapabilityRepository, ProviderRepository, insert_audit

router = APIRouter()


def _repo(request: Request) -> ProviderRepository:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return ProviderRepository(pool)


def _capability_repo(request: Request) -> CapabilityRepository:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return CapabilityRepository(pool)


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return pool


def _provider_to_response(prov: Provider) -> dict[str, Any]:
    return {
        "id": prov.id,
        "capability_id": prov.capability_id,
        "name": prov.name,
        "provider_type": prov.provider_type.value,
        "runpod_endpoint_id": prov.runpod_endpoint_id,
        "runpod_template_id": prov.runpod_template_id,
        "region": prov.region,
        "cloud_type": prov.cloud_type,
        "config": prov.config,
        "priority": prov.priority,
        "enabled": prov.enabled,
        "health_status": prov.health_status,
        "consecutive_failures": prov.consecutive_failures,
        "cooldown_trips": prov.cooldown_trips,
        "cold_start_p50_ms": prov.cold_start_p50_ms,
        "cold_start_p95_ms": prov.cold_start_p95_ms,
        "recent_error_rate": prov.recent_error_rate,
        "cooldown_until": prov.cooldown_until.isoformat() if prov.cooldown_until else None,
        "source": prov.source.value,
        "last_applied_yaml_hash": prov.last_applied_yaml_hash,
        "updated_at": prov.updated_at.isoformat(),
    }


@router.post(
    "/v1/admin/providers",
    status_code=201,
    response_model=ProviderResponse,
)
async def create_provider(
    body: ProviderCreate,
    repo: ProviderRepository = Depends(_repo),
    capability_repo: CapabilityRepository = Depends(_capability_repo),
    pool: Any = Depends(_pool),
) -> dict[str, Any]:
    existing = await repo.get(body.name)
    if existing is not None:
        raise ProviderConflict(body.name)
    capability = await capability_repo.get(body.capability_id)
    if capability is None:
        raise ProviderCapabilityMissing(body.capability_id)

    now = dt.datetime.now(dt.UTC)
    prov_id = f"prov_{ulid_new()}"
    prov = Provider(
        id=prov_id,
        capability_id=body.capability_id,
        name=body.name,
        provider_type=body.provider_type,
        runpod_endpoint_id=body.runpod_endpoint_id,
        runpod_template_id=body.runpod_template_id,
        region=body.region,
        cloud_type=body.cloud_type,
        config=body.config,
        priority=body.priority,
        enabled=body.enabled,
        health_status=body.health_status,
        consecutive_failures=body.consecutive_failures,
        cooldown_trips=body.cooldown_trips,
        cold_start_p50_ms=body.cold_start_p50_ms,
        cold_start_p95_ms=body.cold_start_p95_ms,
        recent_error_rate=body.recent_error_rate,
        cooldown_until=None,
        source=body.source,
        updated_at=now,
    )
    result = await repo.create(prov)
    await insert_audit(
        pool,
        actor="rest:admin",
        action="create",
        entity_type="provider",
        entity_id=result.id,
        new_value={"name": result.name, "capability_id": result.capability_id},
    )
    return _provider_to_response(result)


@router.patch(
    "/v1/admin/providers/{provider_id}",
    response_model=ProviderResponse,
)
async def patch_provider(
    provider_id: str,
    body: ProviderPatch,
    repo: ProviderRepository = Depends(_repo),
    pool: Any = Depends(_pool),
) -> dict[str, Any]:
    existing = await repo.get(provider_id)
    if existing is None:
        raise ProviderNotFound(provider_id)

    old_snapshot = {
        "health_status": existing.health_status,
        "priority": existing.priority,
        "enabled": existing.enabled,
    }
    if (
        body.provider_type is not None
        or body.runpod_endpoint_id is not None
        or body.config is not None
    ):
        provider_type = body.provider_type or existing.provider_type
        endpoint_id = (
            body.runpod_endpoint_id
            if body.runpod_endpoint_id is not None
            else existing.runpod_endpoint_id
        )
        config = body.config if body.config is not None else existing.config
        cloud_type = body.cloud_type if body.cloud_type is not None else existing.cloud_type
        try:
            validate_provider_registration_config(
                provider_type=provider_type,
                endpoint_id=endpoint_id,
                cloud_type=cloud_type,
                config=config,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "loc": ["body", "config"],
                        "msg": str(exc),
                        "type": "value_error",
                    }
                ],
            ) from exc

    result = await repo.patch(
        provider_id,
        name=body.name,
        provider_type=body.provider_type.value if body.provider_type is not None else None,
        runpod_endpoint_id=body.runpod_endpoint_id,
        runpod_template_id=body.runpod_template_id,
        region=body.region,
        cloud_type=body.cloud_type,
        config=body.config,
        priority=body.priority,
        health_status=body.health_status,
        consecutive_failures=body.consecutive_failures,
        cooldown_trips=body.cooldown_trips,
        cold_start_p50_ms=body.cold_start_p50_ms,
        cold_start_p95_ms=body.cold_start_p95_ms,
        recent_error_rate=body.recent_error_rate,
    )
    if result is None:
        raise ProviderNotFound(provider_id)

    new_snapshot = {
        "health_status": result.health_status,
        "priority": result.priority,
        "enabled": result.enabled,
    }
    await insert_audit(
        pool,
        actor="rest:admin",
        action="update",
        entity_type="provider",
        entity_id=provider_id,
        old_value=old_snapshot,
        new_value=new_snapshot,
    )
    return _provider_to_response(result)


@router.post(
    "/v1/admin/providers/{provider_id}/enable",
    response_model=ProviderResponse,
)
async def enable_provider(
    provider_id: str,
    repo: ProviderRepository = Depends(_repo),
    pool: Any = Depends(_pool),
) -> dict[str, Any]:
    existing = await repo.get(provider_id)
    if existing is None:
        raise ProviderNotFound(provider_id)
    result = await repo.enable(provider_id)
    if result is None:
        raise ProviderNotFound(provider_id)
    await insert_audit(
        pool,
        actor="rest:admin",
        action="enable",
        entity_type="provider",
        entity_id=provider_id,
        old_value={"enabled": False},
        new_value={"enabled": True},
    )
    return _provider_to_response(result)


@router.post(
    "/v1/admin/providers/{provider_id}/disable",
    response_model=ProviderResponse,
)
async def disable_provider(
    provider_id: str,
    repo: ProviderRepository = Depends(_repo),
    pool: Any = Depends(_pool),
) -> dict[str, Any]:
    existing = await repo.get(provider_id)
    if existing is None:
        raise ProviderNotFound(provider_id)
    result = await repo.disable(provider_id)
    if result is None:
        raise ProviderNotFound(provider_id)
    await insert_audit(
        pool,
        actor="rest:admin",
        action="disable",
        entity_type="provider",
        entity_id=provider_id,
        old_value={"enabled": True},
        new_value={"enabled": False},
    )
    return _provider_to_response(result)


@router.post(
    "/v1/admin/providers/{provider_id}/hibernate",
    response_model=ProviderHibernateResponse,
)
async def hibernate_provider(
    provider_id: str,
    repo: ProviderRepository = Depends(_repo),
    pool: Any = Depends(_pool),
) -> dict[str, Any]:
    existing = await repo.get(provider_id)
    if existing is None:
        raise ProviderNotFound(provider_id)
    result = await repo.patch(
        provider_id,
        health_status="hibernated",
    )
    if result is None:
        raise ProviderNotFound(provider_id)
    await insert_audit(
        pool,
        actor="rest:admin",
        action="hibernate",
        entity_type="provider",
        entity_id=provider_id,
        old_value={"health_status": existing.health_status},
        new_value={"health_status": "hibernated"},
    )
    return {
        "id": result.id,
        "name": result.name,
        "health_status": result.health_status,
        "cooldown_until": result.cooldown_until.isoformat() if result.cooldown_until else None,
        "enabled": result.enabled,
    }


@router.get(
    "/v1/providers",
)
async def list_providers(
    capability_id: OptionalStrQuery = None,
    enabled: bool | None = None,
    provider_type: ProviderType | None = None,
    repo: ProviderRepository = Depends(_repo),
) -> dict[str, Any]:
    provs = await repo.list(
        capability_id=capability_id,
        enabled_only=enabled is True,
        provider_type=provider_type.value if provider_type is not None else None,
        limit=100,
        offset=0,
    )
    return {
        "items": [_provider_to_response(p) for p in provs],
        "total": len(provs),
    }


@router.get(
    "/v1/providers/{provider_id}",
    response_model=ProviderResponse,
)
async def get_provider(
    provider_id: PathId,
    repo: ProviderRepository = Depends(_repo),
) -> dict[str, Any]:
    prov = await repo.get(provider_id)
    if prov is None:
        raise ProviderNotFound(provider_id)
    return _provider_to_response(prov)


@router.get(
    "/v1/providers/{provider_id}/health",
    response_model=ProviderHealthResponse,
)
async def get_provider_health(
    provider_id: PathId,
    repo: ProviderRepository = Depends(_repo),
) -> dict[str, Any]:
    prov = await repo.get(provider_id)
    if prov is None:
        raise ProviderNotFound(provider_id)
    return {
        "id": prov.id,
        "name": prov.name,
        "health_status": prov.health_status,
        "cooldown_until": prov.cooldown_until.isoformat() if prov.cooldown_until else None,
        "consecutive_failures": prov.consecutive_failures,
        "cooldown_trips": prov.cooldown_trips,
        "recent_error_rate": prov.recent_error_rate,
        "updated_at": prov.updated_at.isoformat(),
    }


__all__ = ["router"]

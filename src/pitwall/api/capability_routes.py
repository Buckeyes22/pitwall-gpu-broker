"""FastAPI route handlers for the Capability CRUD surface.

Admin routes (POST/PATCH) are mounted under ``/v1/admin/capabilities``.
Discovery routes (GET list/detail) are mounted under ``/v1/capabilities``.

Each handler delegates to :class:`pitwall.db.repository.CapabilityRepository`
and raises mapped API exceptions that FastAPI exception handlers translate to
the correct HTTP status codes.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from pitwall.api.capability_schemas import (
    CapabilityCreate,
    CapabilityPatch,
    CapabilityResponse,
)
from pitwall.api.exceptions import CapabilityConflict, CapabilityNotFound
from pitwall.api.schemas.params import PathId
from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode
from pitwall.core.ids import ulid_new
from pitwall.core.models import Capability, CapabilityDefaults
from pitwall.db.repository import CapabilityRepository, insert_audit

router = APIRouter()


def _repo(request: Request) -> CapabilityRepository:
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


def _capability_to_response(cap: Capability) -> dict[str, Any]:
    return {
        "id": cap.id,
        "name": cap.name,
        "version": cap.version,
        "class": cap.class_.value,
        "description": cap.description,
        "input_schema": cap.input_schema,
        "output_schema": cap.output_schema,
        "defaults": cap.defaults.model_dump(mode="json"),
        "cost_mode": cap.cost_mode.value,
        "hints_supported": [h.value for h in cap.hints_supported],
        "source": cap.source.value,
        "last_applied_yaml_hash": cap.last_applied_yaml_hash,
        "enabled": cap.enabled,
        "created_at": cap.created_at.isoformat(),
        "updated_at": cap.updated_at.isoformat(),
    }


@router.post(
    "/v1/admin/capabilities",
    status_code=201,
    response_model=CapabilityResponse,
)
async def create_capability(
    body: CapabilityCreate,
    repo: CapabilityRepository = Depends(_repo),
    pool: Any = Depends(_pool),
) -> dict[str, Any]:
    existing = await repo.get_by_name(body.name)
    if existing is not None:
        raise CapabilityConflict(body.name)

    now = dt.datetime.now(dt.UTC)
    cap_id = f"cap_{ulid_new()}"
    cap = Capability(
        id=cap_id,
        name=body.name,
        version=body.version,
        class_=body.class_,
        description=body.description,
        input_schema=body.input_schema,
        output_schema=body.output_schema,
        defaults=CapabilityDefaults(
            execution_timeout_ms=body.defaults.execution_timeout_ms,
            ttl_ms=body.defaults.ttl_ms,
            result_delivery=body.defaults.result_delivery,
        ),
        cost_mode=body.cost_mode,
        hints_supported=body.hints_supported,
        source=body.source,
        created_at=now,
        updated_at=now,
    )
    result = await repo.create(cap)
    await insert_audit(
        pool,
        actor="rest:admin",
        action="create",
        entity_type="capability",
        entity_id=result.id,
        new_value={"name": result.name, "version": result.version},
    )
    return _capability_to_response(result)


@router.patch(
    "/v1/admin/capabilities/{capability_id}",
    response_model=CapabilityResponse,
)
async def patch_capability(
    capability_id: str,
    body: CapabilityPatch,
    repo: CapabilityRepository = Depends(_repo),
    pool: Any = Depends(_pool),
) -> dict[str, Any]:
    existing = await repo.get(capability_id)
    if existing is None:
        raise CapabilityNotFound(capability_id)

    old_snapshot = {
        "description": existing.description,
        "input_schema": existing.input_schema,
        "output_schema": existing.output_schema,
        "defaults": existing.defaults.model_dump(mode="json"),
    }

    config_patch = body.model_dump(
        exclude_none=True,
        exclude={"name", "version", "class_", "cost_mode"},
    )
    config_payload: dict[str, Any] | None = None
    if config_patch:
        defaults = config_patch.pop("defaults", None)
        if defaults is not None:
            existing.defaults = CapabilityDefaults(**defaults)
        config_payload = {
            "description": config_patch.get("description", existing.description),
            "input_schema": config_patch.get("input_schema", existing.input_schema),
            "output_schema": config_patch.get("output_schema", existing.output_schema),
            "defaults": existing.defaults.model_dump(mode="json"),
            "hints_supported": [
                h.value if hasattr(h, "value") else h
                for h in config_patch.get("hints_supported", existing.hints_supported)
            ],
        }

    result = await repo.patch(
        capability_id,
        name=body.name,
        version=body.version,
        class_=body.class_.value if body.class_ is not None else None,
        cost_mode=body.cost_mode.value if body.cost_mode is not None else None,
        config=config_payload,
    )
    if result is None:
        raise CapabilityNotFound(capability_id)

    new_snapshot = {
        "description": result.description,
        "input_schema": result.input_schema,
        "output_schema": result.output_schema,
        "defaults": result.defaults.model_dump(mode="json"),
    }
    await insert_audit(
        pool,
        actor="rest:admin",
        action="update",
        entity_type="capability",
        entity_id=capability_id,
        old_value=old_snapshot,
        new_value=new_snapshot,
    )
    return _capability_to_response(result)


@router.post(
    "/v1/admin/capabilities/{capability_id}/enable",
    response_model=CapabilityResponse,
)
async def enable_capability(
    capability_id: str,
    repo: CapabilityRepository = Depends(_repo),
    pool: Any = Depends(_pool),
) -> dict[str, Any]:
    existing = await repo.get(capability_id)
    if existing is None:
        raise CapabilityNotFound(capability_id)
    result = await repo.enable(capability_id)
    if result is None:
        raise CapabilityNotFound(capability_id)
    await insert_audit(
        pool,
        actor="rest:admin",
        action="enable",
        entity_type="capability",
        entity_id=capability_id,
        old_value={"enabled": False},
        new_value={"enabled": True},
    )
    return _capability_to_response(result)


@router.post(
    "/v1/admin/capabilities/{capability_id}/disable",
    response_model=CapabilityResponse,
)
async def disable_capability(
    capability_id: str,
    repo: CapabilityRepository = Depends(_repo),
    pool: Any = Depends(_pool),
) -> dict[str, Any]:
    existing = await repo.get(capability_id)
    if existing is None:
        raise CapabilityNotFound(capability_id)
    result = await repo.disable(capability_id)
    if result is None:
        raise CapabilityNotFound(capability_id)
    await insert_audit(
        pool,
        actor="rest:admin",
        action="disable",
        entity_type="capability",
        entity_id=capability_id,
        old_value={"enabled": True},
        new_value={"enabled": False},
    )
    return _capability_to_response(result)


@router.get(
    "/v1/capabilities",
)
async def list_capabilities(
    class_: Annotated[CapabilityClass | None, Query(alias="class")] = None,
    cost_mode: CostMode | None = None,
    source: CapabilitySource | None = None,
    enabled: bool | None = None,
    repo: CapabilityRepository = Depends(_repo),
) -> dict[str, Any]:
    caps = await repo.list(
        enabled_only=enabled is True,
        class_filter=class_.value if class_ is not None else None,
        limit=100,
        offset=0,
    )
    return {
        "items": [_capability_to_response(c) for c in caps],
        "total": len(caps),
    }


@router.get(
    "/v1/capabilities/{name}",
    response_model=CapabilityResponse,
)
async def get_capability(
    name: PathId,
    repo: CapabilityRepository = Depends(_repo),
) -> dict[str, Any]:
    cap = await repo.get_by_name(name)
    if cap is None:
        raise CapabilityNotFound(name)
    return _capability_to_response(cap)


__all__ = ["router"]

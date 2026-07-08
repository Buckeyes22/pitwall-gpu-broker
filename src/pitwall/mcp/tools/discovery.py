"""Discovery tools — capability and provider lookup for the MCP surface.

These tools expose the same read operations as the REST API endpoints:
- GET /v1/capabilities        → pitwall_list_capabilities
- GET /v1/capabilities/{name} → pitwall_describe_capability
- GET /v1/providers            → pitwall_list_providers
- GET /v1/providers/{id}/health→ pitwall_get_provider_health

All handlers delegate to the repository layer without going over HTTP.
Audit context uses actor="mcp" to distinguish MCP-initiated changes from
REST-initiated ones.
"""

from __future__ import annotations

import contextlib
from typing import Any

from pitwall.core.enums import CapabilityClass, CostMode, ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.db import get_pool
from pitwall.db.repository import CapabilityRepository, ProviderRepository


def _capability_to_response(cap: Capability) -> dict[str, Any]:
    """Transform a Capability model to the REST-compatible response shape."""
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


def _provider_to_response(prov: Provider) -> dict[str, Any]:
    """Transform a Provider model to the REST-compatible response shape."""
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


def _parse_capability_class(value: str | None) -> CapabilityClass | None:
    """Parse a capability class string to enum, or return None."""
    if value is None:
        return None
    try:
        return CapabilityClass(value)
    except ValueError:
        return None


def _parse_provider_type(value: str | None) -> ProviderType | None:
    """Parse a provider type string to enum, or return None."""
    if value is None:
        return None
    try:
        return ProviderType(value)
    except ValueError:
        return None


async def pitwall_list_capabilities(
    capability_class: str | None = None,
    cost_mode: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """List all registered capabilities, optionally filtered by class, cost mode, or enabled state.

    Mirrors GET /v1/capabilities with the same filter semantics.
    """
    pool = await get_pool()
    repo = CapabilityRepository(pool)

    class_filter = _parse_capability_class(capability_class)
    if cost_mode:
        with contextlib.suppress(ValueError):
            CostMode(cost_mode)

    caps = await repo.list(
        enabled_only=enabled is True,
        class_filter=class_filter.value if class_filter else None,
        limit=100,
        offset=0,
    )

    return {
        "capabilities": [_capability_to_response(c) for c in caps],
    }


async def pitwall_describe_capability(
    name: str,
) -> dict[str, Any]:
    """Return full details for a single capability by name.

    Mirrors GET /v1/capabilities/{name}.
    Raises CapabilityNotFound if the name does not exist.
    """
    pool = await get_pool()
    repo = CapabilityRepository(pool)

    cap = await repo.get_by_name(name)
    if cap is None:
        from pitwall.api.exceptions import CapabilityNotFound

        raise CapabilityNotFound(name)

    return _capability_to_response(cap)


async def pitwall_list_providers(
    capability_id: str | None = None,
    provider_type: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """List all registered providers, optionally filtered by capability, type, or enabled state.

    Mirrors GET /v1/providers with the same filter semantics.
    """
    pool = await get_pool()
    repo = ProviderRepository(pool)

    type_filter = _parse_provider_type(provider_type)

    provs = await repo.list(
        capability_id=capability_id,
        enabled_only=enabled is True,
        provider_type=type_filter.value if type_filter else None,
        limit=100,
        offset=0,
    )

    return {
        "providers": [_provider_to_response(p) for p in provs],
    }


async def pitwall_get_provider_health(
    provider_id: str,
) -> dict[str, Any]:
    """Return health status, cooldown state, and recent error rate for a single provider.

    Mirrors GET /v1/providers/{id}/health.
    Raises ProviderNotFound if the ID does not exist.
    """
    pool = await get_pool()
    repo = ProviderRepository(pool)

    prov = await repo.get(provider_id)
    if prov is None:
        from pitwall.api.exceptions import ProviderNotFound

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

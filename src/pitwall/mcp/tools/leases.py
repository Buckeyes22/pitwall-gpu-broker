"""Lease tools — pod lease create, get, renew, and stop for the MCP surface.

These tools expose the same lease operations as the REST API endpoints:
- POST /v1/leases          -> pitwall_lease_pod
- GET  /v1/leases/{id}     -> pitwall_get_lease
- POST /v1/leases/{id}/renew -> pitwall_renew_lease
- POST /v1/leases/{id}/stop -> pitwall_stop_lease

All handlers delegate to the same service-layer functions the REST handlers use
(LeaseRepository, run_launch, run_teardown).  Audit context uses actor="mcp" to distinguish
MCP-initiated changes from REST-initiated ones.
"""

from __future__ import annotations

from typing import Any

from pitwall.db import get_pool
from pitwall.db.repository import CapabilityRepository, LeaseRepository, ProviderRepository
from pitwall.leases.mutations import (
    MAX_LEASE_EXPIRY_HORIZON_MINUTES,
    LeaseMutationConflict,
    LeaseMutationExpiryLimitExceeded,
    LeaseMutationIdempotencyConflict,
    LeaseMutationNotFound,
    renew_lease,
)


def _lease_to_response(lease: Any) -> dict[str, Any]:
    return {
        "id": lease.id,
        "provider_id": lease.provider_id,
        "runpod_pod_id": lease.runpod_pod_id,
        "state": lease.state.value if hasattr(lease.state, "value") else lease.state,
        "created_at": lease.created_at.isoformat(),
        "expires_at": lease.expires_at.isoformat(),
        "renewal_policy": (
            lease.renewal_policy.value
            if hasattr(lease.renewal_policy, "value")
            else lease.renewal_policy
        ),
        "auto_teardown_on_expiry": lease.auto_teardown_on_expiry,
        "endpoints": lease.endpoints.model_dump(mode="json") if lease.endpoints else None,
        "readiness": (lease.readiness.model_dump(mode="json") if lease.readiness else None),
        "cost_accrued_usd": (
            str(lease.cost_accrued_usd) if lease.cost_accrued_usd is not None else None
        ),
        "last_health_at": (lease.last_health_at.isoformat() if lease.last_health_at else None),
        "terminated_at": (lease.terminated_at.isoformat() if lease.terminated_at else None),
        "terminated_reason": lease.terminated_reason,
    }


async def pitwall_lease_pod(
    capability_id: str,
    provider_id: str | None = None,
    dry_run: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Create a pod lease for a capability. Routes to a pod_lease provider and tracks readiness.

    Mirrors POST /v1/leases.
    """
    pool = await get_pool()
    capability_repo = CapabilityRepository(pool)
    provider_repo = ProviderRepository(pool)

    capability = await capability_repo.get_by_name(capability_id)
    if capability is None:
        capability = await capability_repo.get(capability_id)
    if capability is None:
        from pitwall.api.exceptions import LeaseNotFound

        raise LeaseNotFound(capability_id)

    provider = None
    if provider_id:
        provider = await provider_repo.get(provider_id)
        if provider is None:
            from pitwall.api.exceptions import LeaseNotFound

            raise LeaseNotFound(provider_id)

    if provider is None:
        providers = await provider_repo.list(
            capability_id=capability.id,
            enabled_only=True,
            limit=1,
        )
        if not providers:
            from pitwall.api.exceptions import ProviderUnavailable

            raise ProviderUnavailable(capability_id)
        provider = providers[0]

    from pitwall.api.leases.launch import run_launch

    result = await run_launch(
        pool=pool,
        capability=capability,
        provider=provider,
        idempotency_key=idempotency_key,
        dry_run=dry_run,
    )

    if dry_run:
        return {
            "id": None,
            "state": "dry_run",
            "dry_run": True,
            "capability_id": capability.id,
            "provider_id": provider.id,
            "template_id": result.get("template_id"),
            "template_name": result.get("template_name"),
        }

    lease_id = result.get("lease_id")
    if lease_id:
        lease_repo = LeaseRepository(pool)
        lease = await lease_repo.get(lease_id)
        if lease is not None:
            return _lease_to_response(lease)

    return result


async def pitwall_get_lease(
    lease_id: str,
) -> dict[str, Any]:
    """Return the current state and details of a pod lease.

    Mirrors GET /v1/leases/{id}.
    """
    pool = await get_pool()
    lease_repo = LeaseRepository(pool)

    lease = await lease_repo.get(lease_id)
    if lease is None:
        from pitwall.api.exceptions import LeaseNotFound

        raise LeaseNotFound(lease_id)

    return _lease_to_response(lease)


async def pitwall_renew_lease(
    lease_id: str,
    extends_minutes: int = 60,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Extend an active pod lease by a number of minutes.

    Mirrors POST /v1/leases/{id}/renew.
    """
    pool = await get_pool()
    lease_repo = LeaseRepository(pool)

    try:
        updated = await renew_lease(
            lease_repo,
            lease_id,
            extends_minutes=extends_minutes,
            actor="mcp",
            idempotency_key=idempotency_key,
        )
    except LeaseMutationNotFound as exc:
        from pitwall.api.exceptions import LeaseNotFound

        raise LeaseNotFound(lease_id) from exc
    except LeaseMutationConflict as exc:
        from pitwall.api.exceptions import LeaseStateConflict

        raise LeaseStateConflict(lease_id, exc.state, exc.operation) from exc
    except LeaseMutationExpiryLimitExceeded as exc:
        from pitwall.api.exceptions import LeaseExpiryLimitExceeded

        raise LeaseExpiryLimitExceeded(lease_id, MAX_LEASE_EXPIRY_HORIZON_MINUTES) from exc
    except LeaseMutationIdempotencyConflict as exc:
        from pitwall.api.exceptions import IdempotencyConflict

        raise IdempotencyConflict(exc.idempotency_key) from exc

    return _lease_to_response(updated)


async def pitwall_stop_lease(
    lease_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Stop and tear down an active pod lease.

    Mirrors POST /v1/leases/{id}/stop.
    """
    pool = await get_pool()

    from pitwall.api.leases.teardown import run_teardown

    result = await run_teardown(
        lease_id=lease_id,
        pool=pool,
        redis_client=None,
        reason=reason,
    )
    return _lease_to_response(result.lease)

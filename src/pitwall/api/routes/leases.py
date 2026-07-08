"""FastAPI route handlers for the Lease surface.

Discovery routes (GET) are mounted under ``/v1/leases``.
Mutating routes (POST/DELETE) are mounted under ``/v1/leases``.

Each handler delegates to :class:`pitwall.db.repository.LeaseRepository`
and raises mapped API exceptions that FastAPI exception handlers translate to
the correct HTTP status codes.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from pitwall.api.exceptions import (
    ChangeSetTooBroad,
    EmptyLeasePatch,
    IdempotencyConflict,
    LeaseExpiryLimitExceeded,
    LeaseNotFound,
    LeaseStateConflict,
    UnsupportedLeasePatch,
)
from pitwall.api.leases.launch import run_launch
from pitwall.api.leases.teardown import run_teardown
from pitwall.api.schemas.leases import (
    LeaseCreate,
    LeasePatch,
    LeaseRenew,
    LeaseResponse,
    LeaseStop,
    lease_patch_conflicting_fields,
    lease_patch_unsupported_fields,
)
from pitwall.api.schemas.params import PathId
from pitwall.core.models import Lease
from pitwall.db.repository import CapabilityRepository, LeaseRepository, ProviderRepository
from pitwall.leases.mutations import (
    LEASE_MUTATION_UNSET,
    MAX_LEASE_EXPIRY_HORIZON_MINUTES,
    LeaseMutationConflict,
    LeaseMutationExpiryLimitExceeded,
    LeaseMutationIdempotencyConflict,
    LeaseMutationNotFound,
    patch_lease_settings,
)
from pitwall.leases.mutations import (
    renew_lease as renew_lease_service,
)

router = APIRouter()


def _map_lease_mutation_error(lease_id: str, exc: RuntimeError) -> None:
    if isinstance(exc, LeaseMutationNotFound):
        raise LeaseNotFound(lease_id) from exc
    if isinstance(exc, LeaseMutationConflict):
        raise LeaseStateConflict(lease_id, exc.state, exc.operation) from exc
    if isinstance(exc, LeaseMutationExpiryLimitExceeded):
        raise LeaseExpiryLimitExceeded(lease_id, MAX_LEASE_EXPIRY_HORIZON_MINUTES) from exc
    if isinstance(exc, LeaseMutationIdempotencyConflict):
        raise IdempotencyConflict(exc.idempotency_key) from exc
    raise exc


def _lease_repo(request: Request) -> LeaseRepository:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return LeaseRepository(pool)


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return pool


def _redis_client(request: Request) -> Any | None:
    return getattr(request.app.state, "redis", None)


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


def _lease_to_response(lease: Lease) -> dict[str, Any]:
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


@router.post(
    "/v1/leases",
    status_code=201,
    response_model=LeaseResponse,
)
async def create_lease(
    body: LeaseCreate,
    pool: Any = Depends(_pool),
    capability_repo: CapabilityRepository = Depends(_capability_repo),
    provider_repo: ProviderRepository = Depends(_provider_repo),
    lease_repo: LeaseRepository = Depends(_lease_repo),
) -> dict[str, Any]:
    capability = await capability_repo.get_by_name(body.capability_id)
    if capability is None:
        raise LeaseNotFound(body.capability_id)

    provider = None
    if body.provider_id:
        provider = await provider_repo.get(body.provider_id)
        if provider is None:
            raise LeaseNotFound(body.provider_id)

    result = await run_launch(
        pool=pool,
        capability=capability,
        provider=provider,
        idempotency_key=body.idempotency_key,
        dry_run=body.dry_run,
    )
    if body.dry_run:
        # Dry-run creates no pod and persists no lease; return the launch plan /
        # cost-estimate result as-is. (Its shape is a separate concern from the
        # LeaseResponse success contract and is not validated against it here.)
        return result

    lease_id = result.get("lease_id")
    lease = await lease_repo.get(lease_id) if lease_id else None
    if lease is None:
        raise RuntimeError(f"lease launch persisted no lease row (lease_id={lease_id!r})")
    return _lease_to_response(lease)


@router.get(
    "/v1/leases/{lease_id}",
    response_model=LeaseResponse,
)
async def get_lease(
    lease_id: PathId,
    repo: LeaseRepository = Depends(_lease_repo),
) -> dict[str, Any]:
    lease = await repo.get(lease_id)
    if lease is None:
        raise LeaseNotFound(lease_id)
    return _lease_to_response(lease)


@router.patch(
    "/v1/leases/{lease_id}",
    response_model=LeaseResponse,
)
async def patch_lease(
    lease_id: PathId,
    body: LeasePatch,
    request: Request,
    repo: LeaseRepository = Depends(_lease_repo),
) -> dict[str, Any]:
    raw_body = await request.json()
    patch_payload: LeasePatch | Mapping[str, object]
    patch_payload = raw_body if isinstance(raw_body, Mapping) else body
    conflicting_fields = lease_patch_conflicting_fields(patch_payload)
    if conflicting_fields:
        raise ChangeSetTooBroad(conflicting_fields)

    unsupported_fields = lease_patch_unsupported_fields(patch_payload)
    if unsupported_fields:
        raise UnsupportedLeasePatch(unsupported_fields)

    supplied_fields = set(body.model_fields_set)
    mutable_fields = supplied_fields & {"renewal_policy", "auto_teardown_on_expiry"}
    if not mutable_fields:
        raise EmptyLeasePatch()

    try:
        updated = await patch_lease_settings(
            repo,
            lease_id,
            renewal_policy=(
                body.renewal_policy if "renewal_policy" in supplied_fields else LEASE_MUTATION_UNSET
            ),
            auto_teardown_on_expiry=(
                body.auto_teardown_on_expiry
                if "auto_teardown_on_expiry" in supplied_fields
                else LEASE_MUTATION_UNSET
            ),
            actor="rest:lease",
            idempotency_key=body.idempotency_key,
        )
    except RuntimeError as exc:
        _map_lease_mutation_error(lease_id, exc)
        raise AssertionError("unreachable") from exc
    return _lease_to_response(updated)


@router.post(
    "/v1/leases/{lease_id}/renew",
    response_model=LeaseResponse,
)
async def renew_lease(
    lease_id: PathId,
    body: LeaseRenew,
    repo: LeaseRepository = Depends(_lease_repo),
) -> dict[str, Any]:
    try:
        updated = await renew_lease_service(
            repo,
            lease_id,
            extends_minutes=body.extends_minutes,
            actor="rest:lease",
            idempotency_key=body.idempotency_key,
        )
    except RuntimeError as exc:
        _map_lease_mutation_error(lease_id, exc)
        raise AssertionError("unreachable") from exc
    return _lease_to_response(updated)


@router.post(
    "/v1/leases/{lease_id}/stop",
    response_model=LeaseResponse,
)
async def stop_lease(
    lease_id: PathId,
    body: LeaseStop | None = None,
    pool: Any = Depends(_pool),
    redis_client: Any | None = Depends(_redis_client),
) -> dict[str, Any]:
    result = await run_teardown(
        lease_id,
        pool=pool,
        redis_client=redis_client,
        reason=body.reason if body is not None else None,
    )
    return _lease_to_response(result.lease)


@router.delete(
    "/v1/leases/{lease_id}",
    status_code=204,
    response_class=Response,
)
async def delete_lease(
    lease_id: PathId,
    pool: Any = Depends(_pool),
    redis_client: Any | None = Depends(_redis_client),
) -> Response:
    with contextlib.suppress(LeaseNotFound):
        await run_teardown(
            lease_id,
            pool=pool,
            redis_client=redis_client,
            reason="delete",
        )
    return Response(status_code=204)


__all__ = ["router"]

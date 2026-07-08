"""Real-Postgres contract tests for atomic, cross-surface lease mutations."""

from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import patch

import pytest

from pitwall.core.enums import LeaseRenewalPolicy, LeaseState
from pitwall.core.models import Lease
from pitwall.db.repository import LeaseRepository
from pitwall.leases.mutations import (
    LEASE_MUTATION_UNSET,
    LeaseMutationIdempotencyConflict,
    patch_lease_settings,
    renew_lease,
)
from tests.api._contract_helpers import build_app, client_for
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.anyio, pytest.mark.integration, requires_pg]


async def _create_lease(
    repo: LeaseRepository,
    lease_id: str,
    *,
    expires_in: dt.timedelta = dt.timedelta(hours=1),
) -> Lease:
    created = dt.datetime.now(dt.UTC)
    return await repo.create(
        Lease(
            id=lease_id,
            provider_id=f"provider_{lease_id}",
            runpod_pod_id=f"pod_{lease_id}",
            state=LeaseState.CREATING,
            created_at=created,
            expires_at=created + expires_in,
            renewal_policy=LeaseRenewalPolicy.MANUAL,
        )
    )


async def test_patch_persists_actual_columns_and_audit(pg_pool) -> None:
    repo = LeaseRepository(pg_pool)
    await _create_lease(repo, "lease_patch_atomic")

    updated = await patch_lease_settings(
        repo,
        "lease_patch_atomic",
        renewal_policy=LEASE_MUTATION_UNSET,
        auto_teardown_on_expiry=False,
        actor="rest:lease",
        idempotency_key="patch-once",
    )

    assert updated.state == LeaseState.CREATING
    assert updated.renewal_policy == LeaseRenewalPolicy.MANUAL
    assert updated.auto_teardown_on_expiry is False
    async with pg_pool.acquire() as conn:
        audit = await conn.fetchrow(
            "SELECT * FROM pitwall.config_audit WHERE entity_id = $1",
            updated.id,
        )
    assert audit is not None
    assert audit["actor"] == "rest:lease"
    assert audit["action"] == "patch"
    assert audit["new_value"] == {"auto_teardown_on_expiry": False}


async def test_concurrent_renewals_accumulate_without_lost_updates(pg_pool) -> None:
    repo = LeaseRepository(pg_pool)
    original = await _create_lease(repo, "lease_concurrent_renew")

    await asyncio.gather(
        *(
            renew_lease(
                repo,
                original.id,
                extends_minutes=1,
                actor="rest:lease",
                idempotency_key=f"concurrent-{index}",
            )
            for index in range(10)
        )
    )

    updated = await repo.get(original.id)
    assert updated is not None
    assert updated.expires_at == original.expires_at + dt.timedelta(minutes=10)
    async with pg_pool.acquire() as conn:
        audit_count = await conn.fetchval(
            "SELECT count(*) FROM pitwall.config_audit WHERE entity_id = $1 AND action = 'renew'",
            original.id,
        )
    assert audit_count == 10


async def test_renewal_retry_is_exactly_once_and_mismatch_conflicts(pg_pool) -> None:
    repo = LeaseRepository(pg_pool)
    original = await _create_lease(repo, "lease_idempotent_renew")

    first = await renew_lease(
        repo,
        original.id,
        extends_minutes=15,
        actor="rest:lease",
        idempotency_key="renew-retry-key",
    )
    replay = await renew_lease(
        repo,
        original.id,
        extends_minutes=15,
        actor="mcp",
        idempotency_key="renew-retry-key",
    )

    assert first.expires_at == original.expires_at + dt.timedelta(minutes=15)
    assert replay.expires_at == first.expires_at
    with pytest.raises(LeaseMutationIdempotencyConflict):
        await renew_lease(
            repo,
            original.id,
            extends_minutes=16,
            actor="rest:lease",
            idempotency_key="renew-retry-key",
        )


async def test_rest_and_mcp_renewal_surfaces_apply_same_contract(pg_pool) -> None:
    repo = LeaseRepository(pg_pool)
    rest_original = await _create_lease(repo, "lease_rest_surface")
    mcp_original = await _create_lease(repo, "lease_mcp_surface")
    app = build_app(pool=pg_pool)

    async with client_for(app) as client:
        response = await client.post(
            f"/v1/leases/{rest_original.id}/renew",
            json={"extends_minutes": 20, "idempotency_key": "rest-surface-renew"},
        )
    assert response.status_code == 200, response.text

    from pitwall.mcp.tools.leases import pitwall_renew_lease

    with patch("pitwall.mcp.tools.leases.get_pool", return_value=pg_pool):
        mcp_response = await pitwall_renew_lease(
            mcp_original.id,
            extends_minutes=20,
            idempotency_key="mcp-surface-renew",
        )

    rest_updated = await repo.get(rest_original.id)
    mcp_updated = await repo.get(mcp_original.id)
    assert rest_updated is not None
    assert mcp_updated is not None
    assert rest_updated.expires_at - rest_original.expires_at == dt.timedelta(minutes=20)
    assert mcp_updated.expires_at - mcp_original.expires_at == dt.timedelta(minutes=20)
    assert response.json()["expires_at"] == rest_updated.expires_at.isoformat()
    assert mcp_response["expires_at"] == mcp_updated.expires_at.isoformat()

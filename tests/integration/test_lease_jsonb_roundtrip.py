"""release program Task 9 fix: an active lease's readiness/endpoints JSONB must survive a
repo round-trip and satisfy the leases_active_readiness_signals CHECK.

Regression for the jsonb double-encoding bug. LeaseRepository wrote
readiness/endpoints via ``model_dump_json()`` (a JSON *string*), and the pool's
jsonb codec encoder (``json.dumps``) re-serialized that string into a JSON
*scalar*. SQL ``readiness ->> 'probe_passed_at'`` then returned NULL, so
promoting a lease to ACTIVE violated the constraint (observed live: every pod
lease 500'd at the active-persist step). The same double-encoding made
readiness/endpoints decode back to None (``_*_from_row`` only accepts dicts).
"""

from __future__ import annotations

import datetime as dt

import pytest

from pitwall.core.enums import LeaseRenewalPolicy, LeaseState
from pitwall.core.models import Lease, LeaseEndpoints, LeaseReadiness
from pitwall.db.repository import LeaseRepository
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.anyio, pytest.mark.integration, requires_pg]


async def test_active_lease_readiness_round_trips_and_satisfies_constraint(pg_pool) -> None:
    repo = LeaseRepository(pg_pool)
    created = dt.datetime.now(dt.UTC)
    lease = Lease(
        id="lease_jsonb_roundtrip",
        provider_id="prov_jsonb_roundtrip",
        runpod_pod_id="pod_jsonb_roundtrip",
        state=LeaseState.CREATING,
        created_at=created,
        expires_at=created + dt.timedelta(hours=1),
        renewal_policy=LeaseRenewalPolicy.MANUAL,
        endpoints=LeaseEndpoints(http={"80": "https://pod-80.proxy.runpod.net"}),
    )
    await repo.create(lease)

    signal_time = created + dt.timedelta(seconds=30)
    readiness = LeaseReadiness(
        runtime_seen_at=signal_time,
        port_mappings_seen_at=signal_time,
        probe_passed_at=signal_time,
        probe_method="runpod_proxy",
    )
    await repo.update_readiness(lease.id, readiness)
    # Promoting to ACTIVE evaluates leases_active_readiness_signals against the
    # stored readiness JSONB; double-encoding makes ->> return NULL -> violation.
    await repo.update_state(lease.id, LeaseState.ACTIVE.value)

    got = await repo.get(lease.id)
    assert got is not None
    assert got.state == LeaseState.ACTIVE
    # readiness/endpoints must decode back as real objects, not None.
    assert got.readiness is not None
    assert got.readiness.has_active_signals
    assert got.endpoints is not None
    assert got.endpoints.http == {"80": "https://pod-80.proxy.runpod.net"}

"""release program Task 3: workload idempotency UNIQUE constraint against real Postgres.

The async path's "same idempotency key => one workload" guarantee is enforced by
a real UNIQUE index on pitwall.workloads(idempotency_key). This proves the DB
rejects a second insert with the same key and that get_by_idempotency_key returns
the original row. Uses the live pg_pool.
"""

from __future__ import annotations

import datetime as dt

import asyncpg
import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, ProviderType, WorkloadState
from pitwall.core.models import Capability, Provider, Workload
from pitwall.db.repository import (
    CapabilityRepository,
    ProviderRepository,
    WorkloadRepository,
)
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.anyio, pytest.mark.integration, requires_pg]
_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)


async def _seed_cap_provider(pool) -> None:
    await CapabilityRepository(pool).create(
        Capability(
            id="cap_bge_m3",
            name="embedding.bge-m3",
            version="1.0.0",
            class_=CapabilityClass.EMBEDDING,
            cost_mode="per_second",
            source=CapabilitySource.API,
            enabled=True,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    await ProviderRepository(pool).create(
        Provider(
            id="prov_bge_m3",
            capability_id="cap_bge_m3",
            name="prov_bge_m3",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="eptest00000000",
            config={"lb_base_url": "https://x.api.runpod.ai"},
            priority=1,
            enabled=True,
            health_status="healthy",
            updated_at=_NOW,
        )
    )


def _workload(wkl_id: str, *, idempotency_key: str | None) -> Workload:
    return Workload(
        id=wkl_id,
        capability_id="cap_bge_m3",
        provider_id="prov_bge_m3",
        type="async",
        state=WorkloadState.QUEUED,
        idempotency_key=idempotency_key,
        submitted_at=_NOW,
    )


async def test_duplicate_idempotency_key_raises_unique_violation(pg_pool) -> None:
    await _seed_cap_provider(pg_pool)
    repo = WorkloadRepository(pg_pool)
    await repo.insert(_workload("wkl_1", idempotency_key="idem-abc"))
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await repo.insert(_workload("wkl_2", idempotency_key="idem-abc"))


async def test_get_by_idempotency_key_returns_original(pg_pool) -> None:
    await _seed_cap_provider(pg_pool)
    repo = WorkloadRepository(pg_pool)
    await repo.insert(_workload("wkl_1", idempotency_key="idem-xyz"))
    found = await repo.get_by_idempotency_key("idem-xyz")
    assert found is not None
    assert found.id == "wkl_1"


async def test_get_by_idempotency_key_missing_returns_none(pg_pool) -> None:
    await _seed_cap_provider(pg_pool)
    repo = WorkloadRepository(pg_pool)
    assert await repo.get_by_idempotency_key("never-used") is None


async def test_null_idempotency_keys_do_not_collide(pg_pool) -> None:
    """The UNIQUE index is partial (WHERE idempotency_key IS NOT NULL), so many
    workloads with a NULL key coexist."""
    await _seed_cap_provider(pg_pool)
    repo = WorkloadRepository(pg_pool)
    await repo.insert(_workload("wkl_1", idempotency_key=None))
    await repo.insert(_workload("wkl_2", idempotency_key=None))
    assert (await repo.get("wkl_1")) is not None
    assert (await repo.get("wkl_2")) is not None

"""release program Task 4: guarded_transition SQL guard against real Postgres.

WorkloadRepository.guarded_transition uses a single UPDATE ... WHERE state IN
(from_states) so a transition only applies when the row is in an expected state.
This proves: a legal transition applies; a from-state mismatch is a no-op (None);
and two concurrent transitions from the same state produce exactly one winner
(the SQL guard, not app logic, serializes them). Uses the live pg_pool.
"""

from __future__ import annotations

import asyncio
import datetime as dt

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


async def _seed_workload(pool, wkl_id: str = "wkl_1", state=WorkloadState.QUEUED) -> None:
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
    await WorkloadRepository(pool).insert(
        Workload(
            id=wkl_id,
            capability_id="cap_bge_m3",
            provider_id="prov_bge_m3",
            type="async",
            state=state,
            submitted_at=_NOW,
        )
    )


async def test_legal_transition_applies(pg_pool) -> None:
    await _seed_workload(pg_pool)
    repo = WorkloadRepository(pg_pool)
    result = await repo.guarded_transition(
        "wkl_1", {WorkloadState.QUEUED.value}, WorkloadState.RUNNING
    )
    assert result is not None
    assert result.state == WorkloadState.RUNNING
    assert (await repo.get("wkl_1")).state == WorkloadState.RUNNING


async def test_from_state_mismatch_is_noop_none(pg_pool) -> None:
    await _seed_workload(pg_pool)  # row is QUEUED
    repo = WorkloadRepository(pg_pool)
    # Guard requires RUNNING, but row is QUEUED -> no update, returns None.
    result = await repo.guarded_transition(
        "wkl_1", {WorkloadState.RUNNING.value}, WorkloadState.COMPLETED
    )
    assert result is None
    assert (await repo.get("wkl_1")).state == WorkloadState.QUEUED


async def test_concurrent_transitions_one_winner(pg_pool) -> None:
    await _seed_workload(pg_pool)
    repo = WorkloadRepository(pg_pool)
    # Two racers both try QUEUED -> RUNNING. Exactly one row-update can match the
    # guard; the loser sees state already changed and returns None.
    results = await asyncio.gather(
        repo.guarded_transition("wkl_1", {WorkloadState.QUEUED.value}, WorkloadState.RUNNING),
        repo.guarded_transition("wkl_1", {WorkloadState.QUEUED.value}, WorkloadState.RUNNING),
    )
    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1
    assert len(losers) == 1
    assert (await repo.get("wkl_1")).state == WorkloadState.RUNNING

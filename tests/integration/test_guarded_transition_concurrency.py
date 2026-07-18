"""Concurrency: two guarded transitions from the same state have one winner."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from pitwall.core.enums import WorkloadState
from pitwall.core.models import Workload
from pitwall.db.repository import WorkloadRepository
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, requires_pg]

_NOW = dt.datetime(2026, 1, 20, 8, 0, 0, tzinfo=dt.UTC)


async def test_guarded_transition_single_winner(
    pg_pool,
    start_gate: asyncio.Event,
) -> None:
    repo = WorkloadRepository(pg_pool)
    await repo.insert(
        Workload(
            id="wkl-gt-race",
            capability_id="cap-1",
            provider_id="prov-1",
            type="inference",
            state=WorkloadState.QUEUED,
            submitted_at=_NOW,
        )
    )

    async def transition():
        await start_gate.wait()
        return await repo.guarded_transition(
            "wkl-gt-race",
            {WorkloadState.QUEUED.value},
            WorkloadState.RUNNING,
        )

    tasks = [asyncio.create_task(transition()) for _ in range(2)]
    start_gate.set()
    a, b = await asyncio.gather(*tasks)

    winners = [r for r in (a, b) if r is not None]
    losers = [r for r in (a, b) if r is None]
    assert len(winners) == 1, "exactly one transition must win"
    assert len(losers) == 1, "the loser must get None"
    assert winners[0].state == WorkloadState.RUNNING

    final = await repo.get("wkl-gt-race")
    assert final is not None
    assert final.state == WorkloadState.RUNNING

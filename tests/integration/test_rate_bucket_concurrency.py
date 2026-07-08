"""Concurrency: rate-bucket consume/429 serialize via row locks."""

from __future__ import annotations

import asyncio

import pytest

from pitwall.rate_limits.store import RateBucketStore
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.anyio, pytest.mark.integration, requires_pg]


async def test_concurrent_consume_never_oversubscribes(
    pg_pool,
    start_gate: asyncio.Event,
) -> None:
    store = RateBucketStore(pg_pool)
    endpoint_id, operation = "ep-1", "create_pod"
    await store.load_or_create(endpoint_id, operation, capacity=5)

    async def consume() -> bool:
        await start_gate.wait()
        _bucket, allowed = await store.atomic_refill_consume(endpoint_id, operation, 1.0)
        return allowed

    tasks = [asyncio.create_task(consume()) for _ in range(20)]
    start_gate.set()
    results = await asyncio.gather(*tasks)
    allowed = sum(1 for r in results if r)

    assert 5 <= allowed <= 6, f"oversubscription: {allowed} allowed"
    final = await store.load(endpoint_id, operation)
    assert final is not None
    assert final.tokens >= 0.0


async def test_record_429_races_with_consumes_and_clamps_capacity(
    pg_pool,
    start_gate: asyncio.Event,
) -> None:
    store = RateBucketStore(pg_pool)
    endpoint_id, operation = "ep-2", "create_pod"
    await store.load_or_create(endpoint_id, operation, capacity=8)

    async def consume() -> bool:
        await start_gate.wait()
        _bucket, allowed = await store.atomic_refill_consume(endpoint_id, operation, 1.0)
        return allowed

    async def record_429() -> None:
        await start_gate.wait()
        await store.record_429(endpoint_id, operation)

    tasks = [asyncio.create_task(consume()) for _ in range(8)]
    tasks.append(asyncio.create_task(record_429()))
    start_gate.set()
    await asyncio.gather(*tasks)

    final = await store.load(endpoint_id, operation)
    assert final is not None
    assert final.capacity == 4
    assert 0.0 <= final.tokens <= 4.0
    assert final.recent_429_at is not None

"""Hermetic tests for async request coalescing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from pitwall.routing.coalescing import AsyncRequestCoalescer

pytestmark = pytest.mark.anyio


async def _release_callers_together(
    count: int,
    call: Callable[[], Awaitable[int]],
) -> list[int]:
    ready = 0
    all_ready = asyncio.Event()

    async def caller() -> int:
        nonlocal ready
        ready += 1
        if ready == count:
            all_ready.set()
        await all_ready.wait()
        return await call()

    tasks = [asyncio.create_task(caller()) for _ in range(count)]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


async def test_concurrent_same_key_shares_one_execution_and_evicts() -> None:
    coalescer = AsyncRequestCoalescer[int]()
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute() -> int:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 200

    async def call() -> int:
        return await coalescer.run("same-key", execute)

    task = asyncio.create_task(_release_callers_together(8, call))
    await started.wait()
    await asyncio.sleep(0)
    release.set()

    results = await task

    assert results == [200] * 8
    assert calls == 1
    assert coalescer.inflight_count == 0


async def test_concurrent_same_key_propagates_failure_to_all_waiters() -> None:
    class UpstreamFailure(Exception):
        pass

    coalescer = AsyncRequestCoalescer[int]()
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute() -> int:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        raise UpstreamFailure("runpod failed")

    async def call() -> int:
        return await coalescer.run("same-key", execute)

    ready = 0
    all_ready = asyncio.Event()

    async def caller() -> int:
        nonlocal ready
        ready += 1
        if ready == 6:
            all_ready.set()
        await all_ready.wait()
        return await call()

    tasks = [asyncio.create_task(caller()) for _ in range(6)]
    await started.wait()
    await asyncio.sleep(0)
    release.set()

    failures = await asyncio.gather(*tasks, return_exceptions=True)
    assert len(failures) == 6
    assert all(isinstance(failure, UpstreamFailure) for failure in failures)
    assert {str(failure) for failure in failures} == {"runpod failed"}
    assert calls == 1
    assert coalescer.inflight_count == 0


async def test_owner_cancellation_does_not_cancel_coalesced_waiter() -> None:
    coalescer = AsyncRequestCoalescer[int]()
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute() -> int:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 200

    owner = asyncio.create_task(coalescer.run("same-key", execute))
    await asyncio.wait_for(started.wait(), timeout=1)
    waiter = asyncio.create_task(coalescer.run("same-key", execute))
    await asyncio.sleep(0)

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    assert coalescer.inflight_count == 1
    assert waiter.done() is False

    release.set()
    assert await asyncio.wait_for(waiter, timeout=1) == 200
    assert calls == 1
    assert coalescer.inflight_count == 0


async def test_different_keys_execute_independently() -> None:
    coalescer = AsyncRequestCoalescer[int]()
    executed: list[int] = []

    async def execute(value: int) -> int:
        executed.append(value)
        await asyncio.sleep(0)
        return value

    first, second = await asyncio.gather(
        coalescer.run("key-a", lambda: execute(1)),
        coalescer.run("key-b", lambda: execute(2)),
    )

    assert {first, second} == {1, 2}
    assert executed == [1, 2]
    assert coalescer.inflight_count == 0
